"""Budgeted executor/transport source proof; no native engine or final settlement claim."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import test_access_runtime as legacy
import test_node_agent as transport

runtime, agent = legacy.runtime, transport.agent
A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


class QuotaPanel(legacy.FakePanel):
    counters = {}
    mutations = []

    def call(self, method, path, payload=None):
        if method == "POST":
            self.mutations.append(path)
        if path.startswith("/panel/api/clients/traffic/"):
            email = path.rsplit("/", 1)[-1]
            if email not in self.counters:
                raise runtime.ProvisionError("Traffic history missing")
            return {"success": True, "obj": {"email": email, **self.counters[email]}}
        if path.startswith("/panel/api/clients/update/"):
            email = path.rsplit("/", 1)[-1]
            client = dict(payload)
            client["uuid"] = client.pop("id")
            client["id"] = 42
            self.clients[email]["client"] = client
            return {"success": True}
        result = super().call(method, path, payload)
        if path == "/panel/api/clients/add":
            self.counters[payload["client"]["email"]] = {"up": 0, "down": 0}
        return result


def budget(**changes):
    return dict(epoch_id=A, baseline_receipt_id="baseline-receipt", credit_receipt_id="credit-receipt1", credit_revision=1,
        counter_baseline_bytes="0", counter_identity_hash=None, counter_source_receipt_id=None, credit_bytes="200", **changes)


class PlacementQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        QuotaPanel.clients = {}; QuotaPanel.counters = {}; QuotaPanel.mutations = []; QuotaPanel.add_calls = 0
        patch = mock.patch.object(runtime, "PanelClient", QuotaPanel); patch.start(); self.addCleanup(patch.stop)
        self.quiesce = mock.patch.object(runtime, "quiesce_placement_panel"); self.quiesce.start(); self.addCleanup(self.quiesce.stop)
        self.config = {"server": {"domain": "entry.example.invalid"},
            "network": {"subscription": {"backend": "xui-native", "path": "/opaque/subscription/"}}}
        self.first = dict(schema_version=3, access_id="access_12345678", placement_id="legacy:access_12345678",
            tenant_id="tenant001", node_id="node0001", command_id="command001", generation=1, entitlement_revision=4,
            plan_hash="a"*64, expected_version=0, desired_version=1, enabled=True, expires_at="2027-01-01T00:00:00Z",
            device_limit=1, quota_bytes="500", operation="access.provision", traffic_budget=budget())

    def update(self, version=2, **changes):
        return dict(self.first, desired_version=version, expected_version=version-1, command_id=f"command{version:03}",
                    operation="access.update_entitlements", **changes)

    def fence(self):
        return json.loads((self.root / "access_12345678.fence").read_text())

    def envelope(self, value=None):
        value = self.first if value is None else value
        remove = {"schema_version", "tenant_id", "node_id", "command_id", "expected_version", "operation"}
        payload = {k: v for k, v in value.items() if k not in remove}
        payload["command_id"] = value["command_id"]
        return dict(schema_version=value["schema_version"], tenant_id=value["tenant_id"], target_node_id=value["node_id"],
            command_id=value["command_id"], expected_version=value["expected_version"], type=value["operation"], attempt=1, payload=payload)

    def test_absolute_share_and_replay_never_apply_the_complete_cap_or_refill(self):
        first = runtime.provision(self.first, self.config, self.root)
        email = first["panel_email"]
        self.assertEqual(QuotaPanel.clients[email]["client"]["totalGB"], 200)
        QuotaPanel.counters[email] = {"up": 40, "down": 10}
        calls = list(QuotaPanel.mutations)
        second = runtime.provision(self.first, self.config, self.root)
        self.assertEqual(first["client_uuid"], second["client_uuid"]); self.assertEqual(QuotaPanel.add_calls, 1)
        self.assertEqual(second, first); self.assertEqual(QuotaPanel.mutations, calls)
        observed = runtime.update_entitlements(self.update(), self.config, self.root)
        self.assertEqual(observed["traffic_counters"][0]["up_bytes"], "40")
        self.assertEqual(QuotaPanel.clients[email]["client"]["totalGB"], 200)
        self.assertEqual(set(second["traffic_counters"][0]), {"identity_hash", "up_bytes", "down_bytes"})

    def precredit_observation(self):
        legacy_request = {k: v for k, v in self.first.items() if k != "traffic_budget"}
        legacy_request["schema_version"] = 2
        first = runtime.provision(legacy_request, self.config, self.root)
        QuotaPanel.counters[first["panel_email"]] = {"up": 40, "down": 10}
        material = runtime.update_entitlements(self.update(2, enabled=False, traffic_budget=None), self.config, self.root)
        self.assertEqual(material["traffic_counters"][0]["up_bytes"], "40")
        self.assertEqual(self.fence()["quota_epochs"], {})
        return first

    def test_precredit_counter_regression_cannot_establish_another_baseline(self):
        first = self.precredit_observation()
        QuotaPanel.counters[first["panel_email"]] = {"up": 39, "down": 10}
        with self.assertRaisesRegex(runtime.ProvisionError, "counter regressed"):
            runtime.update_entitlements(self.update(3, enabled=False, traffic_budget=None), self.config, self.root)

    def test_precredit_identity_loss_preserves_counter_debt_even_when_clients_are_absent(self):
        self.precredit_observation()
        for path in self.root.glob("access_12345678.*.json"):
            path.unlink()
        QuotaPanel.clients.clear()
        with self.assertRaisesRegex(runtime.ProvisionError, "traffic identity disappeared"):
            runtime.update_entitlements(self.update(3, enabled=False, traffic_budget=None), self.config, self.root)

    def test_precredit_highwater_is_checked_before_any_enabled_panel_mutation(self):
        first = self.precredit_observation()
        QuotaPanel.counters[first["panel_email"]] = {"up": 39, "down": 11}
        allocated = {**budget(), "counter_baseline_bytes": "50", "counter_identity_hash": runtime.quota_identity_hash(first),
                     "counter_source_receipt_id": "initial-deny"}
        mutations = list(QuotaPanel.mutations)
        with self.assertRaisesRegex(runtime.ProvisionError, "counter regressed"):
            runtime.update_entitlements(self.update(3, traffic_budget=allocated), self.config, self.root)
        self.assertEqual(QuotaPanel.mutations, mutations)
        self.assertFalse(QuotaPanel.clients[first["panel_email"]]["client"]["enable"])

    def test_topup_increases_absolute_credit_once_without_resetting_stats(self):
        first = runtime.provision(self.first, self.config, self.root); email = first["panel_email"]
        QuotaPanel.counters[email] = {"up": 30, "down": 20}
        next_budget = {**budget(), "credit_revision": 2, "credit_receipt_id": "credit-receipt2", "credit_bytes": "250"}
        command = self.update(traffic_budget=next_budget)
        runtime.update_entitlements(command, self.config, self.root)
        runtime.update_entitlements(command, self.config, self.root)
        self.assertEqual(QuotaPanel.clients[email]["client"]["totalGB"], 250)
        self.assertEqual(QuotaPanel.counters[email], {"up": 30, "down": 20})
        self.assertEqual(len(self.fence()["quota_epochs"]), 1)

    def test_credit_chain_and_unknown_fields_are_rejected_before_panel_mutation(self):
        runtime.provision(self.first, self.config, self.root); before = self.fence(); calls = list(QuotaPanel.mutations)
        for damage in [dict(credit_revision=3), dict(credit_bytes="199"), dict(credit_receipt_id="changed"),
                       dict(credit_bytes="0200"), dict(extra=True), dict(counter_baseline_bytes="1")]:
            with self.subTest(damage=damage), self.assertRaises(runtime.ProvisionError):
                runtime.update_entitlements(self.update(traffic_budget={**budget(), **damage}), self.config, self.root)
            self.assertEqual(self.fence(), before); self.assertEqual(QuotaPanel.mutations, calls)

    def test_exhaustion_never_reenables_and_deny_reports_actual_overrun(self):
        first = runtime.provision(self.first, self.config, self.root); email = first["panel_email"]
        QuotaPanel.counters[email] = {"up": 210, "down": 5}; QuotaPanel.clients[email]["client"]["enable"] = False
        calls = list(QuotaPanel.mutations)
        for _ in range(2):
            with self.assertRaisesRegex(runtime.ProvisionError, "exhausted"):
                runtime.update_entitlements(self.update(), self.config, self.root)
        self.assertEqual(QuotaPanel.mutations, calls); self.assertFalse(QuotaPanel.clients[email]["client"]["enable"])
        material = runtime.update_entitlements(self.update(3, enabled=False), self.config, self.root)
        self.assertEqual(material["traffic_counters"][0]["up_bytes"], "210")
        self.assertTrue(self.fence()["quota_epochs"][A]["terminal"])

    def test_counter_regression_and_automatic_reset_cannot_refill_credit(self):
        first = runtime.provision(self.first, self.config, self.root); email = first["panel_email"]
        QuotaPanel.counters[email] = {"up": 20, "down": 10}
        runtime.update_entitlements(self.update(), self.config, self.root)
        QuotaPanel.counters[email]["up"] = 19
        with self.assertRaisesRegex(runtime.ProvisionError, "regressed"):
            runtime.update_entitlements(self.update(3), self.config, self.root)
        self.assertEqual(self.fence()["quota_epochs"][A]["up_bytes"], "20")
        QuotaPanel.counters[email]["up"] = 20; QuotaPanel.clients[email]["client"]["resetDay"] = 1
        with self.assertRaisesRegex(runtime.ProvisionError, "Automatic traffic reset"):
            runtime.update_entitlements(self.update(3), self.config, self.root)

    def test_missing_identity_or_counter_history_never_recreates_or_assumes_zero(self):
        first = runtime.provision(self.first, self.config, self.root); email = first["panel_email"]
        previous = QuotaPanel.clients.pop(email)
        with self.assertRaises(runtime.ProvisionError):
            runtime.update_entitlements(self.update(), self.config, self.root)
        self.assertEqual(QuotaPanel.add_calls, 1)
        QuotaPanel.clients[email] = previous; QuotaPanel.counters.pop(email)
        with self.assertRaisesRegex(runtime.ProvisionError, "Traffic history missing"):
            runtime.update_entitlements(self.update(3, enabled=False), self.config, self.root)

    def test_adoption_keeps_identity_and_adds_credit_to_verified_counter_baseline(self):
        old = {k: v for k, v in self.first.items() if k != "traffic_budget"}; old.update(schema_version=2, quota_bytes="0")
        first = runtime.provision(old, self.config, self.root); email = first["panel_email"]
        QuotaPanel.counters[email] = {"up": 40, "down": 10}
        denied = runtime.update_entitlements(self.update(2, enabled=False, traffic_budget=None), self.config, self.root)
        identity = denied["traffic_counters"][0]["identity_hash"]
        credit = {**budget(), "counter_identity_hash": identity, "counter_baseline_bytes": "50", "counter_source_receipt_id": "baseline-material"}
        restored = runtime.update_entitlements(self.update(3, traffic_budget=credit), self.config, self.root)
        self.assertEqual(first["client_uuid"], restored["client_uuid"])
        self.assertEqual(QuotaPanel.clients[email]["client"]["totalGB"], 250)
        self.assertEqual(QuotaPanel.counters[email], {"up": 40, "down": 10})

    def test_rotation_requires_deny_and_keeps_old_meter_records_after_cleanup(self):
        first = runtime.provision(self.first, self.config, self.root)
        replacement = dict(self.update(2), operation="access.replace_credential", traffic_budget={**budget(), "epoch_id": B})
        with self.assertRaisesRegex(runtime.ProvisionError, "disabled first"):
            runtime.provision(replacement, self.config, self.root)
        runtime.update_entitlements(self.update(2, enabled=False), self.config, self.root)
        replacement.update(desired_version=3, expected_version=2, command_id="command003")
        second = runtime.provision(replacement, self.config, self.root)
        self.assertNotEqual(first["client_uuid"], second["client_uuid"])
        self.assertEqual(runtime.cleanup_previous(replacement, self.config, self.root), 0)
        self.assertIn(first["panel_email"], QuotaPanel.clients)
        self.assertFalse(QuotaPanel.clients[first["panel_email"]]["client"]["enable"])

    def test_current_and_pinned_pre_budget_executors_cannot_downgrade_even_at_higher_version(self):
        path = Path(__file__).resolve().parents[1]/"fixtures/access_runtime_6228bb7.py"
        pin = json.loads(path.with_suffix(".json").read_text())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), pin["sha256"])
        spec = importlib.util.spec_from_file_location("pre_budget_runtime", path)
        previous = importlib.util.module_from_spec(spec); spec.loader.exec_module(previous)
        with self.assertRaises(previous.ProvisionError): previous.provision(self.first, self.config, self.root)
        runtime.provision(self.first, self.config, self.root)
        old = {k: v for k, v in self.update(99).items() if k != "traffic_budget"}; old.update(schema_version=2, expected_version=1)
        with self.assertRaisesRegex(runtime.ProvisionError, "Pre-budget"):
            runtime.update_entitlements(old, self.config, self.root)
        with self.assertRaises(previous.ProvisionError): previous.update_entitlements(old, self.config, self.root)
        self.assertEqual(QuotaPanel.add_calls, 1)

    def test_transport_requires_budget_and_exact_bound_counter_material(self):
        command = self.envelope(); payload = agent.validate_access_command(command, "node0001", "tenant001")[3]
        material = runtime.provision(self.first, self.config, self.root)
        agent.validate_placement_material(material, payload)
        for changed in [dict(traffic_budget=None), dict(traffic_counters=[]),
                        dict(traffic_counters=[{**material["traffic_counters"][0], "up_bytes": "00"}]),
                        dict(traffic_counters=[{**material["traffic_counters"][0], "up_bytes": "201"}]),
                        dict(traffic_counters=material["traffic_counters"]*2)]:
            with self.subTest(changed=changed), self.assertRaises(agent.AgentError):
                agent.validate_placement_material({**material, **changed}, payload)
        for changed in [dict(traffic_budget=None), dict(traffic_budget={**budget(), "credit_bytes": "501"}),
                        dict(traffic_budget={**budget(), "extra": True})]:
            damaged = {**command, "payload": {**command["payload"], **changed}}
            with self.assertRaises(agent.AgentError): agent.validate_access_command(damaged, "node0001", "tenant001")

    def test_null_budget_keeps_finite_enable_closed_without_minting(self):
        with self.assertRaisesRegex(runtime.ProvisionError, "reserved credit"):
            runtime.provision({**self.first, "traffic_budget": None}, self.config, self.root)
        self.assertEqual(QuotaPanel.add_calls, 0); self.assertEqual(list(self.root.glob("*.json")), [])

    def test_lost_fence_can_deny_but_never_fabricate_verified_quota_history(self):
        runtime.provision(self.first, self.config, self.root)
        (self.root/"access_12345678.fence").unlink()
        denied = runtime.update_entitlements(self.update(2, enabled=False), self.config, self.root)
        self.assertFalse(denied["quota_history_complete"])
        self.assertFalse(self.fence()["quota_history_complete"])
        with self.assertRaisesRegex(runtime.ProvisionError, "history requires reconciliation"):
            runtime.update_entitlements(self.update(3), self.config, self.root)

    def test_malformed_or_foreign_panel_counters_are_not_zero_observations(self):
        first = runtime.provision(self.first, self.config, self.root); email = first["panel_email"]
        for damaged in [{"up": True, "down": 0}, {"up": -1, "down": 0}, {"up": "00", "down": 0},
                        {"up": 0}, {"up": 9223372036854775807, "down": 1}, {"email": "foreign", "up": 0, "down": 0}]:
            QuotaPanel.counters[email] = damaged
            with self.subTest(damaged=damaged), self.assertRaises(runtime.ProvisionError):
                runtime.update_entitlements(self.update(), self.config, self.root)

    def test_completed_receipt_replay_is_immutable_and_does_not_reenable_exhausted_client(self):
        first = runtime.provision(self.first, self.config, self.root); email = first["panel_email"]
        QuotaPanel.counters[email] = {"up": 201, "down": 0}; QuotaPanel.clients[email]["client"]["enable"] = False
        with mock.patch.object(runtime, "PanelClient") as panel:
            self.assertEqual(runtime.provision(self.first, self.config, self.root), first)
            panel.assert_not_called()
        self.assertFalse(QuotaPanel.clients[email]["client"]["enable"])

    def test_lost_create_reply_reconciles_the_same_identity_and_credit(self):
        original = QuotaPanel.call
        def lose_reply(panel, method, path, payload=None):
            result = original(panel, method, path, payload)
            if path == "/panel/api/clients/add":
                raise runtime.ProvisionError("Synthetic response lost after committed create")
            return result
        with mock.patch.object(QuotaPanel, "call", lose_reply), self.assertRaisesRegex(runtime.ProvisionError, "response lost"):
            runtime.provision(self.first, self.config, self.root)
        self.assertFalse(self.fence()["execution_complete"])
        self.assertEqual(QuotaPanel.add_calls, 1)
        result = runtime.provision(self.first, self.config, self.root)
        self.assertEqual(QuotaPanel.add_calls, 1)
        self.assertEqual(QuotaPanel.clients[result["panel_email"]]["client"]["totalGB"], 200)
        self.assertTrue(self.fence()["execution_complete"])

    def test_restore_uses_a_new_reserved_epoch_and_continues_the_same_counter(self):
        first = runtime.provision(self.first, self.config, self.root); email = first["panel_email"]
        QuotaPanel.counters[email] = {"up": 40, "down": 10}
        denied = runtime.update_entitlements(self.update(2, enabled=False), self.config, self.root)
        credit = {**budget(), "epoch_id": B, "counter_baseline_bytes": "50", "credit_bytes": "150",
            "counter_identity_hash": denied["traffic_counters"][0]["identity_hash"], "counter_source_receipt_id": "settlement-material"}
        restored = runtime.update_entitlements(self.update(3, traffic_budget=credit), self.config, self.root)
        self.assertEqual(first["client_uuid"], restored["client_uuid"])
        self.assertEqual(QuotaPanel.clients[email]["client"]["totalGB"], 200)
        self.assertEqual(QuotaPanel.counters[email], {"up": 40, "down": 10})

    def test_credit_cannot_leave_an_older_legacy_identity_enabled(self):
        old = {k: v for k, v in self.first.items() if k != "traffic_budget"}; old.update(schema_version=2, quota_bytes="0")
        runtime.provision(old, self.config, self.root)
        second = runtime.provision({**old, "operation": "access.replace_credential", "desired_version": 2,
            "expected_version": 1, "command_id": "command002"}, self.config, self.root)
        identity = hashlib.sha256(json.dumps([second["panel_email"], second["client_uuid"], second["sub_id"]],
            separators=(",", ":")).encode()).hexdigest()
        credit = {**budget(), "counter_identity_hash": identity, "counter_source_receipt_id": "baseline-material"}
        calls = list(QuotaPanel.mutations)
        with self.assertRaisesRegex(runtime.ProvisionError, "Other placement identities"):
            runtime.update_entitlements(self.update(3, traffic_budget=credit), self.config, self.root)
        self.assertEqual(QuotaPanel.mutations, calls)

    def test_corrupted_cached_material_is_rejected_without_panel_or_state_repair(self):
        runtime.provision(self.first, self.config, self.root)
        path = self.root/"access_12345678.fence"; fence = self.fence()
        fence["quota_material"]["traffic_counters"][0]["up_bytes"] = "1"
        runtime.atomic_json(path, fence)
        with mock.patch.object(runtime, "PanelClient") as panel, self.assertRaisesRegex(runtime.ProvisionError, "receipt is invalid"):
            runtime.provision(self.first, self.config, self.root)
        panel.assert_not_called()

    def test_schema3_requires_actual_budget_runtime_capability(self):
        path = self.root/"runtime.py"; path.write_text("synthetic probe target")
        instance = agent.NodeAgent.__new__(agent.NodeAgent); instance.config = SimpleNamespace(access_runtime_path=path)
        old = {"access_command_schemas": [1, 2], "placement_fence": 3, "disable_absent": True, "unknown_effect_barrier": 1}
        with mock.patch.object(agent, "run_json_command", return_value=old):
            self.assertTrue(instance.placement_runtime_ready()); self.assertFalse(instance.placement_runtime_ready(3))
        path.write_text("changed actual bytes")
        new = {**old, "access_command_schemas": [1, 2, 3], "placement_fence": 4, "traffic_budget": 1, "counter_observations": 1}
        with mock.patch.object(agent, "run_json_command", return_value=new):
            self.assertTrue(instance.placement_runtime_ready()); self.assertTrue(instance.placement_runtime_ready(3))

    def test_agent_cycle_preserves_v3_budget_material_and_attempt_on_placement_path(self):
        command = self.envelope(); calls = []
        instance = agent.NodeAgent.__new__(agent.NodeAgent)
        instance.config = SimpleNamespace(command_mode="access", node_id="node0001", tenant_id="tenant001")
        instance.last_mtls_status = {"state": "SHADOW_ACTIVE"}
        material = runtime.provision(self.first, self.config, self.root)
        def api(method, path, body, **kwargs):
            calls.append((method, path, copy.deepcopy(body)))
            return command if method == "GET" else {}
        instance.mtls_runtime = SimpleNamespace(api_json=api)
        instance.placement_runtime_ready = mock.Mock(return_value=True)
        instance.execute_access_runtime = mock.Mock(return_value=material)
        instance.report_access_command_failure = mock.Mock()
        instance.run_access_command_cycle()
        instance.report_access_command_failure.assert_not_called()
        instance.placement_runtime_ready.assert_called_once_with(3)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1][2], {"attempt": 1})
        self.assertTrue(calls[2][1].endswith("/accesses/access_12345678/placements/legacy:access_12345678/materialize"))
        self.assertEqual(calls[2][2], material)
        self.assertEqual(calls[3][2]["status"], "succeeded")


if __name__ == "__main__": unittest.main()
