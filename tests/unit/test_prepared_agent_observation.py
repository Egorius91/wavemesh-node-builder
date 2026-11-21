"""One full-health stream, durable retries and stock-negative PR4 evidence.

No network, engine, service or native execution guarantee is simulated here.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from test_node_agent import agent


class PreparedAgentObservation(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.node_path = self.directory/"node.json"
        self.node = {"node": {"role": "entry"}, "builder": {"version": "test"}, "routes": [{
            "id": "route-auto-europe", "kind": "auto", "enabled": True,
            "prepared_plan": {"synthetic_private_relay": "private-fixture-value", "address": "192.0.2.40"},
        }]}
        self.write_node()
        self.config = SimpleNamespace(
            runtime_path=self.directory/"runtime.json", node_id="node-entry", tenant_id="tenant-owner",
            auth_mode="mtls", command_mode="observe-only", observed_version=7,
            runtime_finding_mode="disabled", heartbeat_seconds=60, observation_seconds=60,
            public_profiles_enabled=False,
        )
        self.route_health = {"node_status": "healthy", "routes": [{
            "route_id": "route-sentinel", "exit_id": "approved-exit", "enabled": True,
            "status": "healthy", "latency_ms": 12,
        }]}
        self.auto_health = {"auto_routes": [{
            "id": "route-auto-europe", "enabled": True, "published": False, "status": "blocked",
            "strategy": "prepared", "healthy_exits": 0, "total_exits": 2,
        }]}
        self.instance = self.new_instance()
        self.calls = []
        self.instance.api_json = mock.Mock(side_effect=self.accept)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(agent, "NODE_CONFIG_PATH", self.node_path).start()
        self.commands = mock.patch.object(agent, "run_json_command", side_effect=self.health_command).start()

    def write_node(self):
        self.node_path.write_text(json.dumps(self.node), encoding="utf-8")

    def new_instance(self):
        instance = agent.NodeAgent(self.config, mtls_runtime=object())
        instance.last_mtls_status["state"] = "SHADOW_ACTIVE"
        return instance

    def health_command(self, command, timeout):
        self.assertEqual(timeout, 90)
        self.assertEqual(command[-2:], ["health", "--json"])
        return self.auto_health if "auto" in command else self.route_health

    def accept(self, method, path, payload, expected, headers):
        self.calls.append((method, path, copy.deepcopy(payload), copy.deepcopy(headers)))
        self.assertEqual(expected, (202,))
        return {}

    def collect(self, instance=None):
        (instance or self.instance).collect_and_send_observation()

    def row(self, payload=None):
        return (payload or self.calls[-1][2])["state"]["prepared_eu_paths"]["routes"][0]

    def persisted(self):
        return json.loads(self.config.runtime_path.read_text(encoding="utf-8"))

    def create_unknown(self):
        def lost(*args, **kwargs):
            self.accept(*args, **kwargs)
            raise agent.ApiError(503, "PRIVATE_ERROR", True, "private-fixture-value")
        self.instance.api_json.side_effect = lost
        with self.assertRaises(agent.ApiError): self.collect()
        return copy.deepcopy(self.calls[-1])

    def run_once(self):
        with mock.patch.object(agent, "_STOP", False):
            for name in ("rotate_if_due", "run_mtls_lifecycle", "run_access_command_cycle",
                         "send_heartbeat", "run_mtls_shadow_heartbeat", "run_runtime_finding_cycle"):
                mock.patch.object(self.instance, name).start()
            self.instance.run(once=True)

    def test_full_health_payload_is_preserved_and_no_partial_stream_exists(self):
        self.collect()
        self.assertEqual(len(self.calls), 1)
        method, path, payload, headers = self.calls[0]
        self.assertEqual((method, path), ("POST", "internal/v1/nodes/node-entry/observations"))
        expected = agent.build_observation_state(self.route_health, self.auto_health)
        self.assertEqual({k: v for k, v in payload["state"].items() if k != "prepared_eu_paths"}, expected)
        self.assertTrue(payload["type"].startswith("node.health."))
        self.assertTrue(headers["Idempotency-Key"].startswith("node-health-node-entry-"))
        self.assertEqual(payload["observed_version"], 7)
        self.assertEqual(self.row()["state"], "unqualified")
        self.assertEqual(self.row()["deny_outcome"], "not_verified")
        self.assertEqual(self.row()["readback"], "unavailable")
        for key in ("sample_started_at", "sample_completed_at", "engine_instance", "effective_config_sha256",
                    "selected_path", "route_binding", "descriptor", "entry_profile_sha256"):
            self.assertIsNone(self.row()[key])
        serialized = json.dumps(payload)
        for private in ("private-fixture-value", "192.0.2.40", "synthetic_private_relay"):
            self.assertNotIn(private, serialized)
        agent.assert_redacted(payload)

    def test_existing_periodic_owner_sends_one_full_observation(self):
        self.run_once()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.commands.call_count, 2)
        self.instance.run_access_command_cycle.assert_called_once_with()
        self.assertIn("routes", self.calls[0][2]["state"])
        self.assertIn("prepared_eu_paths", self.calls[0][2]["state"])

    def test_unchanged_receipt_does_not_refresh_age_or_send_again(self):
        self.collect()
        original = self.persisted()["last_observation_at"]
        self.collect()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.persisted()["last_observation_at"], original)

    def test_lost_reply_restarts_with_identical_full_body_and_identity(self):
        original = self.create_unknown()
        self.assertEqual(self.persisted()["pending_health_observation"], original[2])
        self.node["routes"][0]["prepared_plan"]["changed"] = True
        self.write_node()
        restarted = self.new_instance()
        restarted.api_json = mock.Mock(side_effect=self.accept)
        self.collect(restarted)
        self.assertEqual(self.calls[-1], original)
        self.assertNotIn("pending_health_observation", self.persisted())
        self.collect(restarted)
        self.assertNotEqual(self.calls[-1][2]["state"], original[2]["state"])
        self.assertIsNone(self.row()["sample_started_at"])

    def test_local_write_failure_cannot_dispatch_an_unpersisted_observation(self):
        with mock.patch.object(agent, "write_json_file", side_effect=OSError("private-fixture-value")):
            with self.assertRaises(OSError): self.collect()
        self.assertEqual(self.calls, [])
        original = copy.deepcopy(self.instance.runtime["pending_health_observation"])
        self.collect()
        self.assertEqual(self.calls[0][2], original)

    def test_other_node_or_tenant_cannot_replay_pending_envelope(self):
        self.create_unknown()
        original = self.persisted()
        for field in ("node_id", "tenant_id"):
            self.instance.runtime = copy.deepcopy(original)
            self.instance.runtime["pending_health_observation_scope"][field] = "other-owner"
            self.instance.api_json.reset_mock()
            with self.assertRaises(agent.AgentError): self.collect()
            self.instance.api_json.assert_not_called()

    def test_cached_positive_stale_future_or_extra_fields_never_send(self):
        self.create_unknown()
        original = self.persisted()
        mutations = [lambda r: r.update(state="qualified_forward"),
                     lambda r: r.update(deny_outcome="verified"),
                     lambda r: r.update(readback="qualified"),
                     lambda r: r.update(sample_started_at=1),
                     lambda r: r.update(sample_started_at=9999999999),
                     lambda r: r.update(engine_instance={"pid": 1}),
                     lambda r: r.update(entry_profile_sha256="f"*64),
                     lambda r: r.update(private_payload="private-fixture-value")]
        for mutate in mutations:
            self.instance.runtime = copy.deepcopy(original)
            pending = self.instance.runtime["pending_health_observation"]
            mutate(self.row(pending))
            identity = agent.health_observation_identity(original["pending_health_observation_scope"], pending["observed_version"], agent.observation_digest(pending["state"]))
            pending["type"] = "node.health."+identity[:16]
            self.instance.api_json.reset_mock()
            with self.assertRaises(agent.AgentError): self.collect()
            self.instance.api_json.assert_not_called()

    def test_partial_cached_health_state_cannot_overwrite_node_state(self):
        self.create_unknown()
        pending = self.instance.runtime["pending_health_observation"]
        pending["state"] = {"prepared_eu_paths": pending["state"]["prepared_eu_paths"]}
        identity = agent.health_observation_identity(self.instance.runtime["pending_health_observation_scope"], pending["observed_version"], agent.observation_digest(pending["state"]))
        pending["type"] = "node.health."+identity[:16]
        self.instance.api_json.reset_mock()
        with self.assertRaises(agent.AgentError): self.collect()
        self.instance.api_json.assert_not_called()

    def test_capability_stays_false_despite_cached_positive_health(self):
        self.instance.last_health_state["prepared_eu_paths"] = {"state": "qualified_forward"}
        self.assertIs(self.instance.build_heartbeat_payload()["capabilities"]["prepared_eu_paths_v1"], False)

    def test_legacy_and_bearer_nodes_keep_full_health_without_route_receipt(self):
        for mode in ("bearer", "bootstrap-mtls"):
            self.config.auth_mode = mode
            self.instance.runtime = {}
            self.collect()
            self.assertNotIn("prepared_eu_paths", self.calls[-1][2]["state"])
            self.assertIs(self.instance.build_heartbeat_payload()["capabilities"]["prepared_eu_paths_v1"], False)
        self.config.auth_mode = "mtls"
        self.node["routes"] = []
        self.write_node()
        self.instance.runtime = {}
        self.collect()
        self.assertEqual(self.calls[-1][2]["state"], agent.build_observation_state(self.route_health, self.auto_health))

    def test_prepared_pending_cannot_downgrade_transport(self):
        self.create_unknown()
        self.config.auth_mode = "bearer"
        self.instance.api_json.reset_mock()
        with self.assertRaises(agent.AgentError): self.collect()
        self.instance.api_json.assert_not_called()

    def test_periodic_errors_use_fixed_code_without_private_exception(self):
        self.instance.api_json.side_effect = OSError("private-fixture-value")
        with self.assertLogs(agent.LOG, level="WARNING") as captured:
            self.run_once()
        self.assertIn("EVIDENCE_UNAVAILABLE", " ".join(captured.output))
        self.assertNotIn("private-fixture-value", " ".join(captured.output))
        self.assertIs(self.instance.build_heartbeat_payload()["capabilities"]["prepared_eu_paths_v1"], False)

    def test_unchanged_health_at_new_configured_version_has_distinct_identity(self):
        self.collect()
        original = copy.deepcopy(self.calls[-1])
        self.config.observed_version = 8
        self.collect()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1][2]["state"], original[2]["state"])
        self.assertEqual(self.calls[-1][2]["observed_version"], 8)
        self.assertNotEqual(self.calls[-1][3], original[3])
        self.assertEqual(self.persisted()["last_observation_version"], 8)
        self.collect()
        self.assertEqual(len(self.calls), 2)

    def test_lost_reply_then_heartbeat_version_advance_can_recover_full_stream(self):
        server = {"version": 7, "events": {}, "lose_first": True}
        def api(method, path, payload, expected, headers=None):
            if path.endswith("/heartbeat"):
                server["version"] = payload["observed_version"]
                return {}
            self.accept(method, path, payload, expected, headers)
            # The real SaaS checks this BEFORE its duplicate-event readback.
            if payload["observed_version"] < server["version"]:
                raise agent.ApiError(409, "STALE_OBSERVED_VERSION", False)
            key = headers["Idempotency-Key"]
            old = server["events"].get(key)
            if old is not None and old != payload:
                raise agent.ApiError(409, "IDEMPOTENCY_KEY_REUSED", False)
            server["events"][key] = copy.deepcopy(payload)
            server["version"] = payload["observed_version"]
            if server["lose_first"]:
                server["lose_first"] = False
                raise agent.ApiError(0, "NETWORK_OR_TLS_ERROR", True)
            return {}
        self.instance.api_json.side_effect = api
        with self.assertRaises(agent.ApiError): self.collect()
        original = copy.deepcopy(self.calls[-1])
        self.config.observed_version = 8
        restarted = self.new_instance()
        restarted.api_json = mock.Mock(side_effect=api)
        restarted.send_heartbeat()
        self.assertEqual(server["version"], 8)
        self.collect(restarted)
        self.assertEqual(self.calls[-1], original)
        receipt = self.persisted()["last_superseded_health_observation"]
        self.assertEqual(receipt["envelope"], original[2])
        self.assertEqual(receipt["idempotency_key"], original[3]["Idempotency-Key"])
        self.assertEqual(receipt["prior_dispatch_outcome"], "UNKNOWN_OR_ACCEPTED")
        self.assertNotIn("pending_health_observation", self.persisted())
        self.collect(restarted)
        self.assertEqual(self.calls[-1][2]["observed_version"], 8)
        self.assertNotEqual(self.calls[-1][3], original[3])
        self.assertEqual(len(server["events"]), 2)
        self.assertIsNone(self.row()["sample_started_at"])

    def test_stale_or_unknown_error_cannot_invent_a_configured_version(self):
        self.create_unknown()
        original = copy.deepcopy(self.instance.runtime)
        for version, error in ((7, agent.ApiError(409, "STALE_OBSERVED_VERSION", False)),
                               (8, agent.ApiError(409, "IDEMPOTENCY_KEY_REUSED", False)),
                               (8, agent.ApiError(0, "STALE_OBSERVED_VERSION", True))):
            self.instance.runtime = copy.deepcopy(original)
            self.config.observed_version = version
            self.instance.api_json.side_effect = error
            with self.assertRaises(agent.ApiError): self.collect()
            self.assertEqual(self.config.observed_version, version)
            self.assertEqual(self.instance.runtime["pending_health_observation"], original["pending_health_observation"])
            self.assertNotIn("last_superseded_health_observation", self.instance.runtime)

    def test_primary_mtls_real_api_error_class_can_confirm_stale_response(self):
        with mock.patch.object(sys, "path", [str(Path(__file__).resolve().parents[2]/"agent"), *sys.path]):
            from node_mtls_client import MtlsApiError
        self.create_unknown()
        self.config.observed_version = 8
        self.instance.mtls_runtime = SimpleNamespace(api_json=mock.Mock(side_effect=MtlsApiError(409, "STALE_OBSERVED_VERSION", False)))
        self.instance.api_json = agent.NodeAgent.api_json.__get__(self.instance)
        with mock.patch.object(agent, "MtlsApiError", MtlsApiError):
            self.collect()
        self.assertNotIn("pending_health_observation", self.persisted())
        self.assertEqual(self.persisted()["last_superseded_health_observation"]["configured_version"], 8)

    def test_unknown_pending_identity_is_not_recomputed_from_new_configuration(self):
        self.create_unknown()
        self.instance.runtime.pop("pending_health_observation_key")
        self.config.observed_version = 8
        self.instance.api_json.reset_mock()
        with self.assertRaises(agent.AgentError): self.collect()
        self.instance.api_json.assert_not_called()
        self.assertIn("pending_health_observation", self.instance.runtime)

    def test_failed_superseded_receipt_save_keeps_original_pending(self):
        self.create_unknown()
        original = copy.deepcopy(self.instance.runtime)
        self.config.observed_version = 8
        self.instance.api_json.side_effect = agent.ApiError(409, "STALE_OBSERVED_VERSION", False)
        write = agent.write_json_file
        def fail_receipt(path, value):
            if "last_superseded_health_observation" in value:
                raise OSError("synthetic persistence failure")
            write(path, value)
        with mock.patch.object(agent, "write_json_file", side_effect=fail_receipt):
            with self.assertRaises(OSError): self.collect()
        self.assertEqual(self.instance.runtime["pending_health_observation"], original["pending_health_observation"])
        self.assertEqual(self.persisted()["pending_health_observation_key"], original["pending_health_observation_key"])
        self.collect()
        self.assertNotIn("pending_health_observation", self.persisted())

    def test_same_health_for_another_tenant_has_another_identity(self):
        self.collect()
        original = copy.deepcopy(self.calls[-1])
        self.config.tenant_id = "tenant-other"
        self.collect()
        self.assertNotEqual(self.calls[-1][3], original[3])
        self.assertEqual(self.persisted()["last_observation_scope"]["tenant_id"], "tenant-other")

    def test_legacy_accepted_hash_does_not_collide_at_same_node_version(self):
        self.node["routes"] = []
        self.write_node()
        state = agent.build_observation_state(self.route_health, self.auto_health)
        state_hash = agent.observation_digest(state)
        legacy_type = "node.health."+state_hash[:16]
        self.instance.runtime = {"last_observation_hash": state_hash, "last_observation_at": "2026-11-01T00:00:00Z"}
        def consumer(method, path, payload, expected, headers):
            # Prisma's existing unique key is nodeId/type/observedVersion.
            if payload["type"] == legacy_type and payload["observed_version"] == 7:
                raise agent.ApiError(409, "IDEMPOTENCY_KEY_REUSED", False)
            return self.accept(method, path, payload, expected, headers)
        self.instance.api_json.side_effect = consumer
        self.collect()
        self.assertNotEqual(self.calls[-1][2]["type"], legacy_type)
        self.assertEqual(self.persisted()["last_observation_version"], 7)

    def test_script_import_uses_actual_primary_mtls_api_error_type(self):
        directory = str(Path(__file__).resolve().parents[2]/"agent")
        code = "import sys;sys.path.insert(0,sys.argv[1]);import node_agent,node_mtls_client;assert node_agent.MtlsApiError is node_mtls_client.MtlsApiError"
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, directory], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__": unittest.main()
