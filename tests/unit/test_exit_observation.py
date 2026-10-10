#!/usr/bin/env python3
"""Exit control health and role-specific observe-only dispatch regressions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("exit_observation_agent", ROOT / "agent/node_agent.py")
assert SPEC and SPEC.loader
agent = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = agent
SPEC.loader.exec_module(agent)
sys.path.insert(0, str(ROOT / "scripts/lib"))
import runtime_state as runtime


class ExitObservationTests(unittest.TestCase):
    def test_actual_common_loader_does_not_write_or_source_state(self):
        bash = shutil.which("bash") or ("C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else None)
        self.assertIsNotNone(bash)
        script = r'''
set -Eeuo pipefail
export PATH="/usr/bin:/bin:$PATH"
python3() { "$TEST_PYTHON" "$@"; }
WM_LIB_DIR="$TEST_ROOT/scripts"
source "$WM_LIB_DIR/00_common.sh"
source "$WM_LIB_DIR/commands/runtime.sh"
WM_STATE_DIR="$TEST_STATE"; WM_CONFIG_JSON="$TEST_STATE/config.json"
wm_xui_request_success() { [[ -z "$PANEL_USERNAME" && -z "$PANEL_PASSWORD" && -z "$CLIENT_UUIDS" ]]; [[ -n "$PANEL_TOKEN" ]] || touch "$TEST_STATE/unexpected-login"; [[ "$*" == 'GET /panel/api/inbounds/list none' ]]; }
systemctl() { return 0; }
wm_xray_process_running() { return 0; }
ss() { printf 'LISTEN 0 10 127.0.0.1:2053\n'; }
openssl() { return 0; }
wm_exit_diagnostics_json
'''
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            config = {
                "schema_version": 2, "node": {"role": "exit"},
                "server": {"domain": "example.invalid"}, "tls": {},
                "panel": {"listen_port": 2053, "path": "/panel/", "api_auth": {"token": "private-fixture-do-not-emit"}, "username": "unused", "password": "unused"},
                "network": {"xhttp": {"port": 10080, "path": "/relay/"}, "subscription": {"path": "/sub/"}},
                "web_identity": {"company_name": "unused"}, "clients": [],
            }
            (state / "runtime.json").write_text('{"unchanged":true}')
            (state / "route.marker").write_text("unchanged")

            def snapshot():
                return {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in state.iterdir() if p.is_file()}

            for env_exists in (True, False):
                with self.subTest(env_exists=env_exists):
                    env = state / "config.env"
                    if env_exists:
                        env.write_text('touch "$TEST_STATE/source-marker"\n')
                        env.chmod(0o640)
                    elif env.exists():
                        env.unlink()
                    (state / "config.json").write_text(json.dumps(config))
                    before = snapshot()
                    done = subprocess.run([bash, "-c", script], env={**os.environ, "TEST_ROOT": ROOT.as_posix(), "TEST_STATE": state.as_posix(), "TEST_PYTHON": Path(sys.executable).as_posix()}, capture_output=True, text=True, timeout=20)
                    self.assertTrue(before == snapshot(), "state bytes/mode/mtime changed")
                    self.assertEqual(done.returncode, 0, "valid Exit config was rejected")
                    self.assertEqual(json.loads(done.stdout)["node_status"], "healthy")
                    self.assertNotIn("private-fixture", done.stdout + done.stderr)

            variants = []
            opaque = json.loads(json.dumps(config))
            opaque["panel"]["path"] = '/$(touch "$TEST_STATE/injection-marker")/`touch "$TEST_STATE/backtick-marker"`/\'quoted/'
            opaque["panel"]["api_auth"]["token"] = opaque["panel"]["path"]
            opaque["panel"]["password"] = {"unused": "never interpreted"}
            opaque["clients"] = [{"uuid": "unused"}]
            variants.append(("opaque-quotes", json.dumps(opaque), "healthy"))
            missing_token = json.loads(json.dumps(config))
            del missing_token["panel"]["api_auth"]["token"]
            variants.append(("missing-bearer", json.dumps(missing_token), "unhealthy"))
            variants.extend([(label, value, None) for label, value in (
                ("missing-file", None), ("malformed-json", '{"private-fixture":'),
                ("array-config", "[]"), ("missing-fields", "{}"),
            )])
            for port in (True, "2053", 0, 65536, None):
                invalid = json.loads(json.dumps(config))
                invalid["panel"]["listen_port"] = port
                variants.append(("invalid-port-" + str(port), json.dumps(invalid), None))
            for field, value in (("path", "no-slash"), ("path", "/bad\0path"), ("path", "/bad\npath"), ("path", None)):
                invalid = json.loads(json.dumps(config))
                invalid["panel"][field] = value
                variants.append(("invalid-path", json.dumps(invalid), None))
            for token in ("bad\0token", "bad\ntoken", None, []):
                invalid = json.loads(json.dumps(config))
                invalid["panel"]["api_auth"]["token"] = token
                variants.append(("invalid-token", json.dumps(invalid), None))
            for role in ("entry", "", None, []):
                invalid = json.loads(json.dumps(config))
                invalid["node"]["role"] = role
                variants.append(("invalid-role", json.dumps(invalid), None))
            invalid = json.loads(json.dumps(config))
            invalid["server"]["domain"] = '../../$(touch "$TEST_STATE/domain-marker")'
            variants.append(("invalid-domain", json.dumps(invalid), None))
            (state / "config.env").write_text('touch "$TEST_STATE/source-marker"\n')
            for label, body, expected in variants:
                with self.subTest(config_case=label):
                    target = state / "config.json"
                    if body is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.write_text(body)
                    before = snapshot()
                    done = subprocess.run([bash, "-c", script], env={**os.environ, "TEST_ROOT": ROOT.as_posix(), "TEST_STATE": state.as_posix(), "TEST_PYTHON": Path(sys.executable).as_posix()}, capture_output=True, text=True, timeout=20)
                    self.assertTrue(before == snapshot(), "state bytes/mode/mtime changed")
                    self.assertNotIn("private-fixture", done.stdout + done.stderr)
                    if expected is None:
                        self.assertNotEqual(done.returncode, 0)
                        self.assertEqual(done.stdout, "")
                    else:
                        self.assertEqual(done.returncode, 0, "valid Exit config was rejected")
                        self.assertEqual(json.loads(done.stdout)["node_status"], expected)

    def test_native_control_health_requires_every_factual_probe(self):
        required = {"service": "active", "api": "reachable", "xray": "running", "panel_bind": "loopback", "bearer": "valid", "nginx": "active", "tls": "valid"}
        self.assertEqual(runtime.control_health({"control": required})["node_status"], "healthy")
        for field in required:
            for fault in (None, False, True, "unknown", "inactive", "private-fixture-do-not-emit"):
                with self.subTest(field=field, fault=fault):
                    probes = {"control": {**required, field: fault}}
                    state = runtime.control_health(probes)
                    self.assertEqual(state["node_status"], "unhealthy")
                    self.assertNotIn("private-fixture", json.dumps(state))
            with self.subTest(missing=field):
                self.assertEqual(runtime.control_health({"control": {key: value for key, value in required.items() if key != field}})["node_status"], "unhealthy")
        for control in (None, [], {}, "healthy"):
            self.assertEqual(runtime.control_health({"control": control, "healthy_exits": 2, "total_exits": 2})["node_status"], "unhealthy")

    def test_exit_native_probe_faults_and_private_response_are_contained(self):
        bash = shutil.which("bash")
        if not bash and os.name == "nt":
            candidate = Path("C:/Program Files/Git/bin/bash.exe")
            bash = str(candidate) if candidate.is_file() else None
        self.assertIsNotNone(bash, "Bash required for native Exit diagnostics proof")
        script = r'''
set -Eeuo pipefail
python3() { "$TEST_PYTHON" "$@"; }
WM_LIB_DIR="$TEST_ROOT/scripts"
source "$WM_LIB_DIR/commands/runtime.sh"
wm_load_exit_diagnostic_config() { NODE_ROLE="${TEST_ROLE:-exit}"; PANEL_TOKEN=private-fixture-do-not-emit; PANEL_PORT=2053; DOMAIN=example.invalid; [[ "$FAULT" != bearer ]] || PANEL_TOKEN=""; }
wm_fail() { printf '%s\n' "$*" >&2; exit 1; }
wm_xui_request_success() { [[ "$*" == 'GET /panel/api/inbounds/list none' ]]; printf 'private-fixture-do-not-emit'; [[ "$FAULT" != api ]]; }
systemctl() { [[ "$*" == 'is-active --quiet x-ui' || "$*" == 'is-active --quiet nginx' ]]; [[ "$FAULT" != "$3" ]]; }
wm_xray_process_running() { [[ "$FAULT" != xray ]]; }
ss() { [[ "$*" == '-ltnH' ]]; if [[ "$FAULT" == panel_bind ]]; then printf 'LISTEN 0 10 0.0.0.0:2053\n'; else printf 'LISTEN 0 10 127.0.0.1:2053\n'; fi; }
openssl() { [[ "$*" == 'x509 -checkend 0 -noout -in /etc/letsencrypt/live/example.invalid/fullchain.pem' ]]; [[ "$FAULT" != tls ]]; }
wm_exit_diagnostics_json
'''
        for fault in ("none", "x-ui", "api", "xray", "panel_bind", "bearer", "nginx", "tls"):
            with self.subTest(fault=fault):
                completed = subprocess.run([bash, "-c", script], env={**os.environ, "TEST_ROOT": ROOT.as_posix(), "TEST_PYTHON": Path(sys.executable).as_posix(), "FAULT": fault}, capture_output=True, text=True, timeout=20)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertNotIn("private-fixture", completed.stdout + completed.stderr)
                state = json.loads(completed.stdout)
                self.assertEqual(state["node_status"], "healthy" if fault == "none" else "unhealthy")
                self.assertEqual(state["routes"], [])
        rejected = subprocess.run([bash, "-c", script], env={**os.environ, "TEST_ROOT": ROOT.as_posix(), "TEST_PYTHON": Path(sys.executable).as_posix(), "FAULT": "none", "TEST_ROLE": "entry"}, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(rejected.stdout, "")

    def test_exit_dispatch_skips_entry_route_checks_and_preserves_disabled_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "agent.env"
            agent.write_env_file(env, {
                "WAVEMESH_API_BASE": "https://example.invalid/api",
                "WAVEMESH_NODE_ID": "node-12345678",
                "WAVEMESH_TENANT_ID": "tenant-12345678",
                "WAVEMESH_AGENT_TOKEN": agent.generate_token(),
                "WAVEMESH_AGENT_TOKEN_EXPIRES_AT": agent.format_timestamp(datetime.now(timezone.utc) + timedelta(hours=1)),
                "WAVEMESH_AGENT_RUNTIME_PATH": str(Path(directory) / "runtime.json"),
            })
            instance = agent.NodeAgent(agent.AgentConfig.load(env))
            for status in ("healthy", "unhealthy"):
                with self.subTest(status=status), mock.patch.object(agent, "read_json_file", return_value={"node": {"role": "exit"}}), mock.patch.object(agent, "run_json_command", return_value={"node_status": status, "routes": [], "panel_token": "private-fixture-do-not-emit"}) as run, mock.patch.object(instance, "api_json") as api:
                    instance.collect_and_send_observation()
                    run.assert_called_once_with(["wavemesh", "diagnostics", "--json"], timeout=90)
                    state = api.call_args.args[2]["state"]
                    self.assertEqual(state["node_status"], status)
                    self.assertEqual(state["routes"], [])
                    self.assertEqual(state["auto_routes"], [])
                    self.assertEqual(state["healthy_exits"], 0)
                    self.assertEqual(state["total_exits"], 0)
                    self.assertNotIn("private-fixture", json.dumps(state))
                    heartbeat = instance.build_heartbeat_payload()
                    self.assertEqual(heartbeat["status"], "active" if status == "healthy" else "degraded")
                    for capability in ("command_polling", "command_execution", "access_lifecycle", "access_entitlements_v2", "access_replacement_prepare_v1"):
                        self.assertFalse(heartbeat["capabilities"][capability])

    def test_entry_dispatch_is_unchanged(self):
        instance = agent.NodeAgent.__new__(agent.NodeAgent)
        instance.config = mock.Mock(node_id="node-12345678", observed_version=0)
        instance.runtime = {}
        instance.api_json = mock.Mock()
        with mock.patch.object(agent, "read_json_file", return_value={"node": {"role": "entry"}}), mock.patch.object(agent, "run_json_command", side_effect=[{"node_status": "healthy", "routes": []}, {"auto_routes": []}]) as run, mock.patch.object(agent, "write_json_file"):
            instance.collect_and_send_observation()
        self.assertEqual(run.call_args_list, [
            mock.call(["wavemesh", "cascade", "health", "--json"], timeout=90),
            mock.call(["wavemesh", "cascade", "auto", "health", "--json"], timeout=90),
        ])


if __name__ == "__main__":
    unittest.main()
