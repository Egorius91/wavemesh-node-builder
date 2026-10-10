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
wm_load_config() { NODE_ROLE="${TEST_ROLE:-exit}"; PANEL_TOKEN=private-fixture-do-not-emit; PANEL_PORT=2053; DOMAIN=example.invalid; [[ "$FAULT" != bearer ]] || PANEL_TOKEN=""; }
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
