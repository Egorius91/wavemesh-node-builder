#!/usr/bin/env python3
"""Exercise the real CLI installation against a disposable loopback panel."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
import access_runtime as runtime
from panel_request_guard import PanelRequestGuard, maintenance_node_lock


class FakePanel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.writes += 1
        if self.server.lose_response:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.respond()

    def do_GET(self):
        self.server.reads += 1
        self.respond()

    def respond(self):
        data = b'{"success":true,"obj":{"fixture":true}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(os.name == "posix", "installed Bash/POSIX transport is exercised on Linux CI")
class InstalledGuardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / "package"
        self.library = self.prefix / "usr/local/lib/wavemesh"
        self.guard = self.library / "lib/panel_request_guard.py"
        self.journal = self.root / "journal"
        result = subprocess.run(["bash", "-c", 'set -Eeuo pipefail; source "$1"; wm_install_cli "$2"',
                                 "fixture", str(ROOT / "scripts/00_common.sh"), str(self.prefix)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePanel)
        self.server.writes, self.server.reads, self.server.lose_response = 0, 0, False
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.env = dict(os.environ, WAVEMESH_PANEL_REQUEST_STATE_DIR=str(self.journal),
                        PANEL_PORT=str(self.server.server_port), PANEL_PATH="/fixture/",
                        PANEL_TOKEN="synthetic_token", XUI_API_TIMEOUT="2",
                        INSTALLED_LIBRARY=str(self.library), CASE_ROOT=str(self.root))

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def shell(self, script):
        return subprocess.run(["bash", "-c", 'set -Eeuo pipefail\n' + script], env=self.env,
                              capture_output=True, timeout=10)

    def request(self, method="POST"):
        path = "/panel/api/clients/add" if method == "POST" else "/panel/api/inbounds/list"
        return self.shell('source "$INSTALLED_LIBRARY/lib/xui_api.sh"\nwm_warn() { :; }\n'
                          f"wm_xui_request {method} {path} json '{{}}'")

    def admission(self):
        return self.shell('WM_STATE_DIR="$CASE_ROOT/state"\n'
                          'source "$INSTALLED_LIBRARY/lib/transaction.sh"\n'
                          'wm_transaction_panel_admission')

    def agent(self):
        return runtime.PanelClient({"panel": {"listen_port": self.server.server_port, "path": "fixture",
                                               "api_auth": {"token": "synthetic_token"}}}, timeout=2)

    def test_install_carries_exact_shared_source_and_no_repo_lookup(self):
        self.assertEqual(self.guard.read_bytes(), (ROOT / "agent/panel_request_guard.py").read_bytes())
        self.assertEqual(self.guard.stat().st_mode & 0o777, 0o644)
        template = self.library / "systemd/50-wavemesh-panel-startup.conf"
        self.assertEqual(template.read_bytes(), (ROOT / "systemd/50-wavemesh-panel-startup.conf").read_bytes())
        self.assertEqual(template.stat().st_mode & 0o777, 0o644)
        self.assertFalse((self.prefix / "etc/systemd").exists())
        result = self.shell('source "$INSTALLED_LIBRARY/lib/xui_api.sh"\n'
                            'printf "%s" "$WM_PANEL_REQUEST_GUARD"')
        self.assertEqual(Path(result.stdout.decode()), self.guard)
        self.assertFalse((self.prefix / "usr/local/lib/agent").exists())
        cli = self.shell('WAVEMESH_LIB_DIR="$INSTALLED_LIBRARY" '
                         '"$CASE_ROOT/package/usr/local/bin/wavemesh" transaction list --json')
        self.assertEqual(cli.returncode, 0, cli.stderr.decode())
        self.assertIn("transactions", json.loads(cli.stdout))
        self.assertEqual(self.admission().returncode, 0)
        self.assertEqual(self.request().returncode, 0)
        self.assertEqual(self.server.writes, 1)

    def test_repository_layout_still_resolves_original_guard(self):
        result = subprocess.run(["bash", "-c", 'source "$1"; printf "%s" "$WM_PANEL_REQUEST_GUARD"',
                                 "fixture", str(ROOT / "scripts/lib/panel_guard.sh")],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(Path(result.stdout.decode()), ROOT / "agent/panel_request_guard.py")

    def test_accepted_installed_cli_and_agent_share_one_journal(self):
        self.assertEqual(self.request().returncode, 0)
        self.assertEqual(self.admission().returncode, 0)
        with patch.dict(os.environ, self.env):
            self.assertTrue(self.agent().call("POST", "/panel/api/clients/add", {})["success"])
        self.assertEqual(self.server.writes, 2)
        self.assertEqual(json.loads((self.journal / "state.json").read_text())["phase"], "RESPONSE_ACCEPTED")

    def test_cli_lost_response_blocks_agent_and_transaction_before_effects(self):
        self.server.lose_response = True
        self.assertNotEqual(self.request().returncode, 0)
        with patch.dict(os.environ, self.env):
            with self.assertRaisesRegex(runtime.ProvisionError, "RECONCILIATION_REQUIRED"):
                self.agent().call("POST", "/panel/api/clients/add", {})
        self.assertNotEqual(self.admission().returncode, 0)
        marker = self.root / "unexpected-snapshot"
        result = self.shell('WM_STATE_DIR="$CASE_ROOT/state"\n'
                            'source "$INSTALLED_LIBRARY/lib/transaction.sh"\n'
                            'wm_fail() { return 1; }; wm_warn() { :; }\n'
                            'wm_transaction_snapshot() { touch "$CASE_ROOT/unexpected-snapshot"; }\n'
                            'if wm_transaction_begin fixture; then exit 20; fi')
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "state/transactions").exists())
        self.assertEqual(self.server.writes, 1)
        self.assertEqual(self.request("GET").returncode, 0)
        self.assertEqual(self.server.reads, 1)

    def test_agent_lost_response_blocks_installed_cli(self):
        self.server.lose_response = True
        with patch.dict(os.environ, self.env):
            with self.assertRaises(runtime.ProvisionError):
                self.agent().call("POST", "/panel/api/clients/add", {})
        self.assertNotEqual(self.request().returncode, 0)
        self.assertNotEqual(self.admission().returncode, 0)
        self.assertEqual(self.server.writes, 1)

    def test_missing_or_symlinked_helper_never_falls_back_or_dispatches(self):
        self.guard.unlink()
        self.assertNotEqual(self.request().returncode, 0)
        self.assertNotEqual(self.admission().returncode, 0)
        self.guard.symlink_to(ROOT / "agent/panel_request_guard.py")
        self.assertNotEqual(self.request().returncode, 0)
        self.assertNotEqual(self.admission().returncode, 0)
        self.assertEqual(self.server.writes, 0)

    def test_reinstall_preserves_uncertain_state_and_keeps_blocking(self):
        self.server.lose_response = True
        self.assertNotEqual(self.request().returncode, 0)
        before = (self.journal / "state.json").read_bytes()
        result = subprocess.run(["bash", "-c", 'set -Eeuo pipefail; source "$1"; wm_install_cli "$2"',
                                 "fixture", str(ROOT / "scripts/00_common.sh"), str(self.prefix)],
                                capture_output=True, env=self.env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(before, (self.journal / "state.json").read_bytes())
        self.assertNotEqual(self.admission().returncode, 0)
        self.assertEqual(self.server.writes, 1)

    def maintenance_cli(self, *args):
        # Relocate only the fixed Node lock in the installed fixture, never
        # introduce a production command-line path override or touch host state.
        node_lock = str(self.root / "node.lock")
        for target in (self.guard, self.library / "lib/transaction.sh"):
            target.write_text(target.read_text().replace("/run/lock/wavemesh-node.lock", node_lock)
                              .replace("mkdir -p /run/lock", ":"))
        return subprocess.run([str(self.prefix / "usr/local/bin/wavemesh"), "maintenance", *args],
                              env=dict(self.env, WAVEMESH_LIB_DIR=str(self.library)),
                              capture_output=True, timeout=10)

    def test_installed_maintenance_commands_block_writes_and_allow_typed_cancellation(self):
        operation = "00000000-0000-4000-8000-000000000001"
        result = self.maintenance_cli("prepare", operation, "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["local_admission"], "CLOSED")
        before = (self.journal / "state.json").read_bytes()
        self.assertNotEqual(self.request().returncode, 0)
        self.assertNotEqual(self.admission().returncode, 0)
        with patch.dict(os.environ, self.env):
            with self.assertRaisesRegex(runtime.ProvisionError, "MAINTENANCE_HELD"):
                self.agent().call("POST", "/panel/api/clients/add", {})
        self.assertEqual(self.request("GET").returncode, 0)
        for action in ("prepare", "status"):
            args = (action, operation, "1") if action == "prepare" else (action,)
            self.assertEqual(self.maintenance_cli(*args).returncode, 0)
        self.assertEqual(before, (self.journal / "state.json").read_bytes())
        self.assertEqual(self.server.writes, 0)
        self.assertEqual(self.maintenance_cli("cancel", operation, "1").returncode, 0)
        self.assertEqual(self.request().returncode, 0)
        self.assertEqual(self.server.writes, 1)

    def test_installed_cli_rejects_untyped_actions_without_network(self):
        for args in (("reset",), ("prepare",), ("status", "extra"),
                     ("prepare", "bad", "1"), ("cancel", "bad", "-1"),
                     ("prepare", "00000000-0000-4000-8000-000000000001", "1", "--force")):
            with self.subTest(args=args):
                result = self.maintenance_cli(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertNotIn(b"Traceback", result.stderr)
        self.assertEqual(self.server.writes, 0)
        self.assertFalse((self.journal / "state.json").exists())

    def test_held_maintenance_stops_cli_lock_and_repair_before_commands(self):
        operation = "00000000-0000-4000-8000-000000000001"
        self.assertEqual(self.maintenance_cli("prepare", operation, "1").returncode, 0)
        commands = self.root / "commands"
        commands.mkdir()
        for name in ("nginx", "systemctl", "certbot"):
            executable = commands / name
            executable.write_text('#!/bin/sh\ntouch "$CASE_ROOT/unexpected-effect"\n')
            executable.chmod(0o755)
        for option in ("--nginx", "--ssl"):
            result = subprocess.run([str(self.prefix / "usr/local/bin/wavemesh"), "repair", option],
                                    env=dict(self.env, WAVEMESH_LIB_DIR=str(self.library),
                                             PATH=str(commands) + os.pathsep + self.env["PATH"]),
                                    capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / "unexpected-effect").exists())
        result = self.shell('WM_STATE_DIR="$CASE_ROOT/state"\n'
                            'source "$INSTALLED_LIBRARY/lib/transaction.sh"\n'
                            'wm_fail() { return 1; }\n'
                            'wm_lock_mutation fixture\n'
                            'touch "$CASE_ROOT/unexpected-effect"')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "unexpected-effect").exists())

    def test_maintenance_reinstall_retains_hold(self):
        guard = PanelRequestGuard(self.journal)
        with maintenance_node_lock(self.root / "node.lock"), guard.locked():
            guard.maintenance("prepare", "00000000-0000-4000-8000-000000000001", 1)
        before = (self.journal / "state.json").read_bytes()
        result = subprocess.run(["bash", "-c", 'set -Eeuo pipefail; source "$1"; wm_install_cli "$2"',
                                 "fixture", str(ROOT / "scripts/00_common.sh"), str(self.prefix)],
                                capture_output=True, env=self.env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, (self.journal / "state.json").read_bytes())
        self.assertNotEqual(self.admission().returncode, 0)
        self.assertNotEqual(self.request().returncode, 0)
        self.assertEqual(self.server.writes, 0)

    def test_installation_intent_blocks_installed_cancel_agent_and_cli_after_reinstall(self):
        operation = "00000000-0000-4000-8000-000000000001"
        self.assertEqual(self.maintenance_cli("prepare", operation, "1").returncode, 0)
        guard = PanelRequestGuard(self.journal)
        with guard.installation_intent(operation, 1, "a" * 64, "b" * 64, self.root / "node.lock"):
            pass
        before = (self.journal / "state.json").read_bytes()
        result = subprocess.run(["bash", "-c", 'set -Eeuo pipefail; source "$1"; wm_install_cli "$2"',
                                 "fixture", str(ROOT / "scripts/00_common.sh"), str(self.prefix)],
                                capture_output=True, env=self.env, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertNotEqual(self.maintenance_cli("cancel", operation, "1").returncode, 0)
        status = self.maintenance_cli("status")
        self.assertEqual(status.returncode, 0)
        self.assertEqual(json.loads(status.stdout)["installation"]["phase"], "INSTALL_INTENT")
        self.assertNotEqual(self.request().returncode, 0)
        self.assertNotEqual(self.admission().returncode, 0)
        with patch.dict(os.environ, self.env):
            with self.assertRaisesRegex(runtime.ProvisionError, "MAINTENANCE_HELD"):
                self.agent().call("POST", "/panel/api/clients/add", {})
        self.assertEqual((self.journal / "state.json").read_bytes(), before)
        self.assertEqual(self.server.writes, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
