from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
import test_access_runtime as fixtures
from test_access_entitlements import node_agent

runtime = fixtures.runtime


class ReplacementPrepareTests(unittest.TestCase):
    def setUp(self):
        fixtures.FakePanel.clients = {}
        fixtures.FakePanel.add_calls = 0
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.request = {
            "operation": "access.prepare_replacement",
            "replacement_id": "replace_12345678",
            "access_id": "access_12345678", "desired_version": 2, "enabled": False,
            "expires_at": "2020-01-01T00:00:00Z", "device_limit": 2, "quota_bytes": "0",
        }
        self.config = {"server": {"domain": "entry.example.invalid"},
                       "network": {"subscription": {"backend": "xui-native", "path": "/opaque/subscription/"}}}

    def execute(self, panel=fixtures.FakePanel):
        with mock.patch.object(runtime, "PanelClient", panel):
            return runtime.provision(self.request, self.config, self.root)

    def command(self):
        return dict(command_id="command_12345678", attempt=1, schema_version=1,
                    target_node_id="node_12345678", type=self.request["operation"],
                    payload={k: v for k, v in self.request.items() if k != "operation"})

    def test_disabled_expired_candidate_replays_without_links_or_second_create(self):
        class Panel(fixtures.FakePanel):
            def call(self, method, path, payload=None):
                if "/subLinks/" in path:
                    raise AssertionError("Disabled candidate must not require published links")
                return super().call(method, path, payload)
        first = self.execute(Panel)
        self.assertEqual(first, self.execute(Panel))
        self.assertEqual(Panel.add_calls, 1)
        self.assertIs(Panel.clients[first["panel_email"]]["client"]["enable"], False)

    def test_lost_create_response_preserves_disabled_identity(self):
        class Panel(fixtures.FakePanel):
            def call(self, method, path, payload=None):
                result = super().call(method, path, payload)
                if path == "/panel/api/clients/addDisabled":
                    raise runtime.ProvisionError("lost response")
                return result
        with self.assertRaises(runtime.ProvisionError):
            self.execute(Panel)
        material = self.execute(Panel)
        self.assertEqual(Panel.add_calls, 1)
        self.assertIs(Panel.clients[material["panel_email"]]["client"]["enable"], False)

    def test_legacy_panel_is_rejected_without_unsafe_create_or_fallback(self):
        calls = []
        class LegacyPanel(fixtures.FakePanel):
            def call(self, method, path, payload=None):
                calls.append((method, path))
                if path == "/panel/api/clients/addDisabled":
                    raise runtime.ProvisionError("404 unsupported")
                return super().call(method, path, payload)
        with self.assertRaises(runtime.ProvisionError):
            self.execute(LegacyPanel)
        self.assertEqual(LegacyPanel.clients, {})
        self.assertEqual(LegacyPanel.add_calls, 0)
        self.assertEqual([path for method, path in calls if method == "POST"],
                         ["/panel/api/clients/addDisabled"])

    def test_enabled_readback_is_not_silently_compensated_and_accepted(self):
        class BrokenPanel(fixtures.FakePanel):
            def call(self, method, path, payload=None):
                if path == "/panel/api/clients/addDisabled":
                    return super().call(method, "/panel/api/clients/add", payload)
                if method == "POST":
                    raise AssertionError("must not hide an enabled creation with a later disable")
                return super().call(method, path, payload)
        with self.assertRaisesRegex(runtime.ProvisionError, "Disabled creation contract violated"):
            self.execute(BrokenPanel)

    def test_enabled_or_missing_operation_identity_rejected_before_panel(self):
        for patch in ({"enabled": True}, {"enabled": 0}, {"replacement_id": "../unsafe"}, {"replacement_id": None}):
            with self.subTest(patch=patch), mock.patch.object(runtime, "PanelClient") as panel:
                with self.assertRaises(runtime.ProvisionError):
                    runtime.provision({**self.request, **patch}, self.config, self.root)
                panel.assert_not_called()

    def test_rebinding_same_version_to_another_operation_is_rejected(self):
        self.execute()
        self.request["replacement_id"] = "replace_87654321"
        with self.assertRaises(runtime.ProvisionError):
            self.execute()
        self.assertEqual(fixtures.FakePanel.add_calls, 1)

    def test_agent_payload_rejects_enabled_and_missing_replacement_id(self):
        node_agent.validate_access_command(self.command(), "node_12345678")
        for patch in ({"enabled": True}, {"enabled": 0}, {"replacement_id": "../unsafe"}):
            command = self.command()
            command["payload"].update(patch)
            with self.assertRaises(node_agent.AgentError):
                node_agent.validate_access_command(command, "node_12345678")
        command = self.command()
        del command["payload"]["replacement_id"]
        with self.assertRaises(node_agent.AgentError):
            node_agent.validate_access_command(command, "node_12345678")

    def test_material_is_operation_scoped_and_failed_receipt_never_reports_success(self):
        for fail_receipt in (False, True):
            with self.subTest(fail_receipt=fail_receipt):
                agent = object.__new__(node_agent.NodeAgent)
                agent.config = SimpleNamespace(command_mode="access", node_id="node_12345678")
                agent.last_mtls_status = {"state": "SHADOW_ACTIVE"}
                calls = []
                def api(method, path, body, **kwargs):
                    calls.append((path, body))
                    if method == "GET": return self.command()
                    if path.endswith("/materialize") and fail_receipt:
                        raise RuntimeError("receipt lost")
                agent.mtls_runtime = SimpleNamespace(api_json=api)
                agent.execute_access_runtime = mock.Mock(return_value={"synthetic": "material"})
                agent.cleanup_replaced_access_runtime = mock.Mock()
                agent.report_access_command_failure = mock.Mock()
                agent.run_access_command_cycle()
                paths = [path for path, body in calls]
                self.assertIn("internal/v1/nodes/node_12345678/replacements/replace_12345678/materialize", paths)
                self.assertFalse(any("/accesses/" in path for path in paths))
                results = [body for path, body in calls if path.endswith("/result")]
                self.assertEqual(len(results), 0 if fail_receipt else 1)
                self.assertEqual(agent.report_access_command_failure.call_count, int(fail_receipt))
                agent.cleanup_replaced_access_runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
