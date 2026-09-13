from pathlib import Path
import tempfile
import unittest
from unittest import mock
import test_access_runtime as fixtures

runtime = fixtures.runtime


class ProvisionEntitlementsTests(unittest.TestCase):
    def setUp(self):
        fixtures.FakePanel.clients = {}
        fixtures.FakePanel.add_calls = 0
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.command = {"access_id": "access_12345678", "desired_version": 1, "enabled": True,
                        "expires_at": "2030-01-01T00:00:00Z", "device_limit": 2, "quota_bytes": "0"}
        self.config = {"server": {"domain": "entry.example.invalid"},
                       "network": {"subscription": {"backend": "xui-native", "path": "/opaque/subscription/"}}}

    def execute(self, panel):
        with mock.patch.object(runtime, "PanelClient", panel):
            return runtime.provision(self.command, self.config, self.root)

    def test_initial_provision_repairs_ignored_entitlements_and_reads_back_before_material(self):
        updates = []
        class Panel(fixtures.FakePanel):
            def call(self, method, path, payload=None):
                if path.startswith("/panel/api/clients/update/"):
                    email = path.rsplit("/", 1)[-1]
                    updates.append(payload)
                    self.clients[email] = {"client": dict(payload), "inboundIds": [9]}
                    return {"success": True}
                result = super().call(method, path, payload)
                if path == "/panel/api/clients/add":
                    self.clients[payload["client"]["email"]]["client"].update(
                        {"enable": False, "expiryTime": 1, "limitIp": 99, "totalGB": 1024})
                return result
        first = self.execute(Panel)
        self.assertEqual(self.execute(Panel), first)
        self.assertEqual(len(updates), 1)
        client = Panel.clients[first["panel_email"]]["client"]
        self.assertIs(client["enable"], True)
        self.assertEqual(client["expiryTime"], 1893456000000)
        self.assertEqual(client["limitIp"], 2)
        self.assertEqual(client["totalGB"], 0)

    def test_ignored_repair_never_returns_material(self):
        links = []
        class Panel(fixtures.FakePanel):
            def call(self, method, path, payload=None):
                if path.startswith("/panel/api/clients/update/"):
                    return {"success": True}
                if "/subLinks/" in path: links.append(path)
                result = super().call(method, path, payload)
                if path == "/panel/api/clients/add":
                    self.clients[payload["client"]["email"]]["client"]["enable"] = False
                return result
        with self.assertRaises(runtime.ProvisionError):
            self.execute(Panel)
        self.assertEqual(links, [])

    def test_lost_create_response_reuses_identity_and_does_not_add_twice(self):
        class Panel(fixtures.FakePanel):
            def call(self, method, path, payload=None):
                result = super().call(method, path, payload)
                if path == "/panel/api/clients/add":
                    raise runtime.ProvisionError("lost response")
                return result
        with self.assertRaises(runtime.ProvisionError):
            self.execute(Panel)
        material = self.execute(Panel)
        self.assertEqual(Panel.add_calls, 1)
        self.assertIn(material["panel_email"], Panel.clients)


if __name__ == "__main__":
    unittest.main()
