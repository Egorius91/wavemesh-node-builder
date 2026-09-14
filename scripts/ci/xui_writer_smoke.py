"""Real Agent/installed CLI transports for the private candidate smoke only."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("smoke_access_runtime", ROOT / "agent/access_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class Writers:
    def __init__(self, root, port, token, require):
        self.require = require
        self.root = root
        self.prefix = root / "cli-package"
        self.library = self.prefix / "usr/local/lib/wavemesh"
        self.node_lock = root / "node.lock"
        self.journal = root / "panel-journal"
        self.config = {"panel": {"listen_port": port, "path": "smoke", "api_auth": {"token": token}}}
        self.env = {"PATH": os.environ["PATH"], "HOME": str(root), "LANG": "C.UTF-8",
                    "WAVEMESH_PANEL_REQUEST_STATE_DIR": str(self.journal),
                    "WM_STATE_DIR": str(root / "node-state"),
                    "WAVEMESH_LIB_DIR": str(self.library), "PANEL_PORT": str(port),
                    "PANEL_PATH": "/smoke/", "PANEL_TOKEN": token, "XUI_API_TIMEOUT": "3"}
        result = subprocess.run(["bash", "-c", 'set -Eeuo pipefail; source "$1"; wm_install_cli "$2"',
                                 "fixture", str(ROOT / "scripts/00_common.sh"), str(self.prefix)],
                                env=self.env, capture_output=True, timeout=10)
        require(result.returncode == 0, "CLI_FIXTURE_INSTALL_FAILED")
        guard = self.library / "lib/panel_request_guard.py"
        require(guard.read_bytes() == (ROOT / "agent/panel_request_guard.py").read_bytes(),
                "INSTALLED_GUARD_SOURCE_MISMATCH")
        # Relocate fixed host lock paths only in the disposable installed fixture.
        # The Agent uses its existing explicit lock argument. No runtime bypass or
        # production command-line path override is added.
        for target in (guard, self.library / "lib/transaction.sh"):
            target.write_text(target.read_text().replace("/run/lock/wavemesh-node.lock", str(self.node_lock))
                              .replace("mkdir -p /run/lock", ":"))

    @contextmanager
    def environment(self):
        name = "WAVEMESH_PANEL_REQUEST_STATE_DIR"
        previous = os.environ.get(name)
        os.environ[name] = str(self.journal)
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    def agent(self, path, payload, lose_response=False):
        with self.environment(), runtime.node_mutation_lock(self.node_lock):
            client = runtime.PanelClient(self.config, timeout=3)
            if lose_response:
                dispatch = client._call

                def discard_response(*args):
                    # The real panel must accept/commit first. Only delivery back
                    # to the journal is lost, simulating timeout after a write.
                    dispatch(*args)
                    raise TimeoutError("synthetic response loss")

                client._call = discard_response
            return client.call("POST", path, payload)

    def agent_rejected(self, path, payload, code, lose_response=False):
        try:
            self.agent(path, payload, lose_response)
        except runtime.ProvisionError as exc:
            self.require(str(exc) == code, "AGENT_REJECTION_REASON_MISMATCH")
        else:
            self.require(False, "AGENT_WRITE_NOT_REJECTED")

    def cli(self, path, payload):
        # Credentials/payload/output stay in the private process/fixture, never
        # the workflow log. This is the installed production Bash transport.
        payload_file = self.root / "cli-payload.json"
        payload_file.write_text(json.dumps(payload))
        return subprocess.run(["bash", "-c", '''set -Eeuo pipefail
source "$WAVEMESH_LIB_DIR/lib/transaction.sh"
source "$WAVEMESH_LIB_DIR/lib/xui_api.sh"
wm_fail() { return 1; }
wm_warn() { :; }
wm_lock_mutation smoke
wm_xui_request_success POST "$1" json "$(cat "$2")"
''', "fixture", path, str(payload_file)], env=self.env, capture_output=True, timeout=10)

    def maintenance(self, action, operation=None, generation=None, accepted=True):
        args = [str(self.prefix / "usr/local/bin/wavemesh"), "maintenance", action]
        if operation is not None:
            args.extend([operation, str(generation)])
        result = subprocess.run(args, env=self.env, capture_output=True, timeout=10)
        self.require((result.returncode == 0) is accepted, "MAINTENANCE_RESULT_MISMATCH")
        return json.loads(result.stdout) if accepted else None

    def journal_bytes(self):
        return (self.journal / "state.json").read_bytes()
