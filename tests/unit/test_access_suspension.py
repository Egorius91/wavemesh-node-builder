from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_access_entitlements as fixtures

runtime = fixtures.runtime


class SuspensionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccessEntitlementTests()
        self.fixture.setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.fixture.seed(self.root, expiry_ms=1000, limit_ip=1, total_gb=0)
        self.request = {**self.fixture.request(), "enabled": False}

    def execute(self, request=None, panel=fixtures.FakePanel):
        with mock.patch.object(runtime, "PanelClient", panel):
            return runtime.update_entitlements(request or self.request, self.fixture.config(), self.root)

    def test_disable_retry_restore_preserves_identity_and_fences_stale_disable(self):
        disabled = self.execute()
        email = disabled["panel_email"]
        self.assertIs(fixtures.FakePanel.clients[email]["enable"], False)
        self.assertEqual(self.execute(), disabled)
        self.assertEqual(len(fixtures.FakePanel.updates), 1)
        restored = self.execute({**self.request, "desired_version": 5, "enabled": True})
        self.assertIs(fixtures.FakePanel.clients[email]["enable"], True)
        self.assertEqual(restored["client_uuid"], disabled["client_uuid"])
        self.assertEqual(restored["subscription_url"], disabled["subscription_url"])
        with self.assertRaisesRegex(runtime.ProvisionError, "stale"):
            self.execute()
        self.assertIs(fixtures.FakePanel.clients[email]["enable"], True)

    def test_lost_disable_response_is_reconciled_by_readback(self):
        class LostResponse(fixtures.FakePanel):
            def call(self, method, path, payload=None):
                result = super().call(method, path, payload)
                if method == "POST":
                    raise runtime.ProvisionError("response lost")
                return result
        self.execute(panel=LostResponse)
        self.execute(panel=LostResponse)
        self.assertEqual(len(fixtures.FakePanel.updates), 1)

    def test_ignored_disable_is_failure_and_never_uses_additive_fallback(self):
        fixtures.FakePanel.sticky_update_fields = {"enable"}
        with self.assertRaises(runtime.ProvisionError):
            self.execute()
        self.assertEqual(fixtures.FakePanel.adjustments, [])

    def test_disabled_links_may_be_empty_but_restoration_requires_links(self):
        class NoLinks(fixtures.FakePanel):
            def call(self, method, path, payload=None):
                if "/subLinks/" in path:
                    return {"success": True, "obj": []}
                return super().call(method, path, payload)
        self.execute(panel=NoLinks)
        with self.assertRaisesRegex(runtime.ProvisionError, "links"):
            self.execute({**self.request, "desired_version": 5, "enabled": True}, panel=NoLinks)

    def test_disabled_provision_and_nonboolean_enabled_are_rejected(self):
        agent = fixtures.node_agent
        payload = {key: value for key, value in self.request.items() if key != "operation"}
        command = {"command_id": "command_12345678", "schema_version": 1,
                   "target_node_id": "node-12345678", "type": "access.update_entitlements",
                   "attempt": 1, "payload": payload}
        agent.validate_access_command(command, "node-12345678")
        for kind in ["access.provision", "access.replace_credential"]:
            with self.assertRaises(agent.AgentError):
                agent.validate_access_command({**command, "type": kind}, "node-12345678")
        for value in [0, 1, "false", None]:
            with self.assertRaises(agent.AgentError):
                agent.validate_access_command({**command, "payload": {**payload, "enabled": value}}, "node-12345678")


if __name__ == "__main__":
    unittest.main()
