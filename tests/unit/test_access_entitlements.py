#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]

RUNTIME_SPEC = importlib.util.spec_from_file_location(
    "wave_access_runtime_entitlements",
    ROOT / "agent" / "access_runtime.py",
)
assert RUNTIME_SPEC and RUNTIME_SPEC.loader
runtime = importlib.util.module_from_spec(RUNTIME_SPEC)
sys.modules[RUNTIME_SPEC.name] = runtime
RUNTIME_SPEC.loader.exec_module(runtime)

AGENT_SPEC = importlib.util.spec_from_file_location(
    "wave_node_agent_entitlements",
    ROOT / "agent" / "node_agent.py",
)
assert AGENT_SPEC and AGENT_SPEC.loader
node_agent = importlib.util.module_from_spec(AGENT_SPEC)
sys.modules[AGENT_SPEC.name] = node_agent
AGENT_SPEC.loader.exec_module(node_agent)


class FakePanel:
    clients: dict[str, dict] = {}
    updates: list[tuple[str, dict]] = []

    def __init__(self, _config, timeout=20):
        self.timeout = timeout

    def call(self, method, path, payload=None):
        if method == "GET" and path == "/panel/api/inbounds/list":
            return {
                "success": True,
                "obj": [
                    {"id": 3, "enable": True, "remark": "public", "protocol": "vless"},
                ],
            }
        if method == "GET" and path.startswith("/panel/api/clients/get/"):
            email = path.rsplit("/", 1)[-1]
            client = self.clients.get(email)
            if client is None:
                raise runtime.ProvisionError("missing")
            return {"success": True, "obj": {"client": dict(client), "inboundIds": [3]}}
        if method == "POST" and path.startswith("/panel/api/clients/update/"):
            email = path.rsplit("/", 1)[-1]
            self.updates.append((email, dict(payload or {})))
            self.clients[email] = dict(payload or {})
            return {"success": True, "obj": {}}
        if method == "GET" and path.startswith("/panel/api/clients/subLinks/"):
            return {"success": True, "obj": ["vless://redacted"]}
        raise AssertionError((method, path, payload))


class AccessEntitlementTests(unittest.TestCase):
    def setUp(self) -> None:
        FakePanel.clients = {}
        FakePanel.updates = []

    def test_runtime_updates_entitlements_without_rotating_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory)
            previous = {
                "access_id": "access_12345678",
                "desired_version": 3,
                "panel_email": "wm_access_12345678_3",
                "client_uuid": "65fecc02-7371-4e2c-b8dc-049c99a5f090",
                "sub_id": "subscription_12345678",
            }
            runtime.atomic_json(state_root / "access_12345678.3.json", previous)
            FakePanel.clients[previous["panel_email"]] = {
                "email": previous["panel_email"],
                "id": previous["client_uuid"],
                "subId": previous["sub_id"],
                "limitIp": 1,
                "totalGB": 1073741824,
                "expiryTime": int(datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp() * 1000),
                "enable": True,
                "flow": "",
            }
            request_value = {
                "operation": "access.update_entitlements",
                "access_id": "access_12345678",
                "desired_version": 4,
                "enabled": True,
                "expires_at": "2026-10-05T09:19:08.033Z",
                "device_limit": 2,
                "quota_bytes": "2147483648",
            }
            config = {
                "network": {
                    "subscription": {"backend": "xui-native", "path": "sub-secure-native"},
                },
                "server": {"domain": "entry.example.invalid"},
            }

            with mock.patch.object(runtime, "PanelClient", FakePanel):
                material = runtime.update_entitlements(request_value, config, state_root)

            current = json.loads((state_root / "access_12345678.4.json").read_text())
            self.assertEqual(current["panel_email"], previous["panel_email"])
            self.assertEqual(current["client_uuid"], previous["client_uuid"])
            self.assertEqual(current["sub_id"], previous["sub_id"])
            self.assertEqual(material["desired_version"], 4)
            self.assertEqual(material["client_uuid"], previous["client_uuid"])
            self.assertEqual(len(FakePanel.updates), 1)
            update = FakePanel.updates[0][1]
            self.assertEqual(update["limitIp"], 2)
            self.assertEqual(update["totalGB"], 2147483648)
            self.assertEqual(
                update["expiryTime"],
                int(datetime.fromisoformat("2026-10-05T09:19:08.033+00:00").timestamp() * 1000),
            )

    def test_agent_allowlists_entitlement_command_and_passes_operation_to_runtime(self) -> None:
        payload = {
            "access_id": "access_12345678",
            "desired_version": 4,
            "enabled": True,
            "expires_at": "2026-10-05T09:19:08.033Z",
            "device_limit": 1,
            "quota_bytes": "0",
        }
        command = {
            "command_id": "command_12345678",
            "schema_version": 1,
            "target_node_id": "node-12345678",
            "type": "access.update_entitlements",
            "attempt": 1,
            "payload": payload,
        }
        validated = node_agent.validate_access_command(command, "node-12345678")
        self.assertEqual(validated[2], "access.update_entitlements")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "access_runtime.py"
            executable.write_text("# runtime placeholder\n", encoding="utf-8")
            config = SimpleNamespace(
                access_runtime_path=executable,
                access_state_root=root / "state",
                env_path=root / "agent.env",
            )
            instance = object.__new__(node_agent.NodeAgent)
            instance.config = config
            captured: dict = {}

            def fake_run(argv, **_kwargs):
                request_path = Path(argv[argv.index("--request") + 1])
                output_path = Path(argv[argv.index("--output") + 1])
                captured.update(json.loads(request_path.read_text(encoding="utf-8")))
                node_agent.write_json_file(
                    output_path,
                    {
                        "desired_version": 4,
                        "panel_email": "wm_access_12345678_3",
                        "client_uuid": "65fecc02-7371-4e2c-b8dc-049c99a5f090",
                        "sub_id": "subscription_12345678",
                        "primary_inbound_id": 3,
                        "protocol": "vless",
                        "subscription_url": "https://entry.example.invalid/sub/subscription_12345678",
                    },
                )
                return SimpleNamespace(returncode=0)

            with mock.patch.object(node_agent.subprocess, "run", fake_run):
                material = instance.execute_access_runtime("access.update_entitlements", payload)

            self.assertEqual(captured["operation"], "access.update_entitlements")
            self.assertEqual(material["desired_version"], 4)


if __name__ == "__main__":
    unittest.main()
