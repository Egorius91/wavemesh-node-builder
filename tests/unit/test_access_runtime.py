from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "agent" / "access_runtime.py"
SPEC = importlib.util.spec_from_file_location("wave_access_runtime", MODULE_PATH)
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)


class FakePanel:
    clients: dict[str, dict] = {}
    add_calls = 0

    def __init__(self, _config):
        pass

    def call(self, method, path, payload=None):
        if path == "/panel/api/inbounds/list":
            return {
                "success": True,
                "obj": [
                    {"id": 9, "enable": True, "protocol": "vless", "remark": "Public"},
                    {"id": 10, "enable": True, "protocol": "vless", "remark": "--!Hidden"},
                ],
            }
        if path.startswith("/panel/api/clients/get/"):
            email = path.rsplit("/", 1)[-1]
            value = self.clients.get(email)
            if value is None:
                raise runtime.ProvisionError("missing")
            return {"success": True, "obj": value}
        if path == "/panel/api/clients/add":
            self.__class__.add_calls += 1
            client = dict(payload["client"])
            client_uuid = client.pop("id")
            client["id"] = 42
            client["uuid"] = client_uuid
            self.clients[client["email"]] = {
                "client": client,
                "inboundIds": list(payload["inboundIds"]),
            }
            return {"success": True}
        if path.startswith("/panel/api/clients/subLinks/"):
            return {"success": True, "obj": ["vless://not-logged"]}
        raise AssertionError(path)


class AccessRuntimeTests(unittest.TestCase):
    def setUp(self):
        FakePanel.clients = {}
        FakePanel.add_calls = 0

    def test_provision_is_idempotent_and_filters_hidden_inbounds(self):
        command = {
            "access_id": "access_12345678",
            "desired_version": 1,
            "enabled": True,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(days=1)
            ).isoformat(),
            "device_limit": 1,
            "quota_bytes": "1073741824",
        }
        config = {
            "panel": {
                "listen_port": 54321,
                "path": "/opaque-panel/",
                "api_auth": {"token": "safe_token_12345678"},
            },
            "server": {"domain": "entry.example.invalid"},
            "network": {
                "subscription": {
                    "backend": "xui-native",
                    "path": "/opaque/subscription/",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            runtime, "PanelClient", FakePanel
        ):
            first = runtime.provision(command, config, Path(directory))
            second = runtime.provision(command, config, Path(directory))
            state_text = next(Path(directory).glob("*.json")).read_text(encoding="utf-8")

        self.assertEqual(first, second)
        self.assertEqual(FakePanel.add_calls, 1)
        self.assertEqual(first["primary_inbound_id"], 9)
        self.assertNotIn("vless://", state_text)
        self.assertNotIn("subscription_url", state_text)

    def test_visible_inbounds_exclude_disabled_hidden_and_non_vless(self):
        result = runtime.visible_vless_inbound_ids({
            "obj": [
                {"id": 1, "enable": True, "protocol": "vless", "remark": "one"},
                {"id": 2, "enable": False, "protocol": "vless", "remark": "two"},
                {"id": 3, "enable": True, "protocol": "vless", "remark": "--!three"},
                {"id": 4, "enable": True, "protocol": "trojan", "remark": "four"},
            ]
        })
        self.assertEqual(result, [1])


if __name__ == "__main__":
    unittest.main()
