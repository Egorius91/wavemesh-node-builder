"""Synthetic evidence only; no service, live panel, certificate or VPN traffic."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
import runtime_findings as findings
import node_agent as agent


class Panel:
    def __init__(self):
        self.calls = []
        self.client = {"client": {"email": "synthetic-orphan", "uuid": "9a8b5032-59a1-4d11-8fd7-8d0599067904",
                        "subId": "synthetic_private_subid_123", "enable": True, "expiryTime": 0, "limitIp": 0}, "inboundIds": [9]}
        self.names = [{"email": "synthetic-orphan"}]
        self.reads = 0
        self.drift = False

    def call(self, method, path, payload=None):
        assert method == "GET" and payload is None
        self.calls.append(path)
        if path == "/panel/api/clients/list":
            return {"success": True, "obj": deepcopy(self.names)}
        if path == "/panel/api/inbounds/list":
            return {"success": True, "obj": [{"id": 9, "enable": True, "protocol": "vless", "remark": "Public"}]}
        if path == "/panel/api/clients/get/synthetic-orphan":
            self.reads += 1
            result = deepcopy(self.client)
            if self.drift and self.reads == 2:
                result["client"]["limitIp"] = 8
            return {"success": True, "obj": result}
        raise AssertionError("Unexpected synthetic GET")


class Api:
    def __init__(self):
        self.calls = []
        self.fail = None

    def api_json(self, method, path, payload, expected):
        assert method == "POST" and path == "internal/v1/nodes/node_fixture/runtime-findings" and expected == (202,)
        self.calls.append(deepcopy(payload))
        if self.fail:
            raise self.fail
        return {"accepted": True, "finding_id": "finding_fixture_accepted", "disposition": "OBSERVED_ONLY", "observed_at": payload["observed_at"], "report_sha256": findings.digest(payload)}


class ContractTests(unittest.TestCase):
    def test_report_hash_matches_typescript_receiver_vector(self):
        value = {"schema_version": 1, "kind": "UNMANAGED_CLIENT_CANDIDATE",
                 "finding_ref": "11111111-1111-4111-8111-111111111111", "revision": "22222222-2222-4222-8222-222222222222",
                 "baseline_sha256": "a" * 64, "observed_at": "2026-09-14T09:00:00.000Z"}
        self.assertEqual(findings.digest(value), "31279d7ce1187386c6718a7de57ed0c3106239b7da789a2c43bdecdf4ad0bb27")

    def test_read_only_adapter_rejects_writes_subscription_reads_and_arbitrary_paths(self):
        panel = findings.ReadOnlyPanel({"panel": {"listen_port": 23456, "path": "/fixture", "api_auth": {"token": "synthetic_token_123"}}})
        for method, path, payload in [("POST", "/panel/api/clients/list", None), ("GET", "/panel/api/clients/list", {}),
                                      ("GET", "/panel/api/clients/subLinks/private", None), ("GET", "/arbitrary", None),
                                      ("GET", "/panel/api/clients/get/..", None), ("GET", "/panel/api/clients/get/%2Fetc", None)]:
            with self.assertRaisesRegex(findings.FindingError, "PANEL_OPERATION_FORBIDDEN"):
                panel.call(method, path, payload)

    def test_client_inventory_rejects_duplicate_case_missing_and_unsafe_names(self):
        for rows in [[{"email": "synthetic-client"}, {"email": "SYNTHETIC-client"}], [{}], [{"email": "../../private"}], [None]]:
            with self.assertRaises(findings.FindingError):
                findings.client_names({"success": True, "obj": rows})

    def test_read_only_transport_bounds_body_and_disables_redirects(self):
        panel = findings.ReadOnlyPanel({"panel": {"listen_port": 23456, "path": "/fixture", "api_auth": {"token": "synthetic_token_123"}}})
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"x" * (findings.MAX_BYTES + 1)
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(findings.request, "build_opener", return_value=opener) as build:
            with self.assertRaises(findings.FindingError):
                panel.call("GET", "/panel/api/clients/list")
            response.read.assert_called_once_with(findings.MAX_BYTES + 1)
            handler = build.call_args.args[0]
            self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://other.invalid"))


@unittest.skipUnless(os.name == "posix", "POSIX private state/flock contract")
class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.json"
        self.config = {"panel": {"listen_port": 23456, "path": "/fixture", "api_auth": {"token": "synthetic_token_123"}}}
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)
        self.access = self.root / "access"
        self.access.mkdir(mode=0o700)
        self.journal = self.root / "findings"
        self.lock = self.root / "node.lock"
        self.now = 1_789_380_000.0
        self.panel, self.api = Panel(), Api()
        self.engine = self.new_engine()

    def new_engine(self, node_id="node_fixture"):
        return findings.RuntimeFindingCycle(node_id, "tenant_fixture", self.journal, self.config_path, self.access,
            clock=lambda: self.now, panel_factory=lambda _config: self.panel, mutation_lock_path=self.lock)

    def collect(self):
        return self.engine.cycle(None)

    def record(self):
        return self.engine.records()[0]

    def save_access(self, value):
        path = self.access / "fixture.1.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def test_private_capture_and_delivery_never_expose_client_material(self):
        self.assertEqual(self.collect(), {"state": "QUEUED"})
        record = self.record()
        self.assertIn("synthetic_private_subid_123", json.dumps(record["baseline"]))
        self.assertEqual(record["report"]["baseline_sha256"], findings.digest(record["baseline"]))
        self.assertEqual(len(record["baseline"]["nonce"]), 64)
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o700)
        for path in self.journal.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        result = self.engine.cycle(self.api)
        self.assertEqual(result, {"state": "OBSERVED_ONLY"})
        self.assertEqual(self.record()["delivery"]["phase"], "ACCEPTED")
        public = json.dumps([self.api.calls, result])
        for secret in ("synthetic-orphan", "synthetic_private_subid_123", self.panel.client["client"]["uuid"], "synthetic_token_123", record["baseline"]["nonce"]):
            self.assertNotIn(secret, public)
        self.assertEqual(len(self.panel.calls), 5)

    def test_lost_response_restart_reuses_exact_report_even_after_freshness_window(self):
        self.collect()
        original = deepcopy(self.record()["report"])
        self.api.fail = TimeoutError("synthetic_private_subid_123")
        self.assertEqual(self.engine.cycle(self.api)["state"], "RETRY_PENDING")
        self.assertEqual(self.record()["delivery"]["attempts"], 1)
        self.assertEqual(self.engine.cycle(self.api)["state"], "RETRY_PENDING")
        self.assertEqual(len(self.api.calls), 1)
        self.now += 601
        self.api.fail = None
        self.panel.call = mock.Mock(side_effect=AssertionError("must not recollect"))
        restarted = self.new_engine()
        self.assertEqual(restarted.cycle(None)["state"], "QUEUED")
        self.assertEqual(restarted.cycle(self.api)["state"], "OBSERVED_ONLY")
        self.assertEqual(self.api.calls, [original, original])
        self.assertEqual(self.record()["report"], original)

    def test_process_crash_after_dispatch_journal_retries_same_envelope(self):
        class Crash(BaseException):
            pass
        self.collect()
        self.api.fail = Crash()
        with self.assertRaises(Crash):
            self.engine.cycle(self.api)
        self.assertEqual(self.record()["delivery"]["phase"], "IN_FLIGHT")
        self.api.fail = None
        self.now += 31
        self.assertEqual(self.new_engine().cycle(self.api)["state"], "OBSERVED_ONLY")
        self.assertEqual(self.api.calls[0], self.api.calls[1])

    def test_ack_disk_failure_keeps_original_report_for_reconciliation(self):
        self.collect()
        save = self.engine.save
        def fail_ack(record):
            if record["delivery"]["phase"] == "ACCEPTED":
                raise OSError("synthetic disk failure")
            save(record)
        with mock.patch.object(self.engine, "save", side_effect=fail_ack):
            with self.assertRaises(OSError):
                self.engine.cycle(self.api)
        self.assertEqual(self.record()["delivery"]["phase"], "IN_FLIGHT")
        self.now += 31
        self.new_engine().cycle(self.api)
        self.assertEqual(self.api.calls[0], self.api.calls[1])

    def test_fsync_failure_before_dispatch_sends_nothing(self):
        self.collect()
        original = self.record()
        with mock.patch.object(findings.os, "fsync", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                self.engine.cycle(self.api)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.record(), original)

    def test_retry_budget_survives_new_instances_without_new_revisions(self):
        self.collect()
        self.api.fail = TimeoutError("private canary")
        for _ in range(findings.MAX_ATTEMPTS):
            self.new_engine().cycle(self.api)
            self.now += 901
        self.assertEqual(self.new_engine().cycle(self.api)["state"], "REVIEW_REQUIRED")
        self.assertEqual(self.new_engine().cycle(None)["state"], "REVIEW_REQUIRED")
        self.assertEqual(len(self.api.calls), findings.MAX_ATTEMPTS)
        self.assertTrue(all(body == self.api.calls[0] for body in self.api.calls))
        self.assertEqual(len(self.engine.records()), 1)

    def test_definitive_stale_response_does_not_refresh_or_drop_evidence(self):
        self.collect()
        original = deepcopy(self.record()["report"])
        failure = RuntimeError("secret response must not be retained")
        failure.status = 409
        self.api.fail = failure
        self.assertEqual(self.engine.cycle(self.api)["state"], "REVIEW_REQUIRED")
        self.now += 1000
        self.assertEqual(self.new_engine().cycle(None)["state"], "REVIEW_REQUIRED")
        self.assertEqual(self.record()["report"], original)
        self.assertNotIn(str(failure), json.dumps(self.record()))

    def test_invalid_or_reordered_receipt_is_not_acceptance(self):
        self.collect()
        receipt = {"accepted": True, "finding_id": "finding_fixture_accepted", "disposition": "OBSERVED_ONLY", "observed_at": self.record()["report"]["observed_at"]}
        for result in [{"accepted": True}, receipt, {**receipt, "report_sha256": "b" * 64},
                       {**receipt, "observed_at": "2020-01-01T00:00:00.000Z", "report_sha256": findings.digest(self.record()["report"])}]:
            api = SimpleNamespace(api_json=mock.Mock(return_value=result))
            self.assertEqual(self.engine.cycle(api)["state"], "RETRY_PENDING")
            self.now += 901
        self.assertNotEqual(self.record()["delivery"]["phase"], "ACCEPTED")

    def test_old_accepted_evidence_is_not_refreshed_on_unchanged_reads(self):
        self.collect()
        self.engine.cycle(self.api)
        original = deepcopy(self.record())
        self.now += 1000
        self.assertEqual(self.new_engine().cycle(None)["state"], "IDLE")
        self.assertEqual(self.record(), original)
        self.assertEqual(len(self.engine.records()), 1)

    def test_changed_baseline_keeps_finding_ref_but_old_revision_immutable(self):
        self.collect()
        self.engine.cycle(self.api)
        original = deepcopy(self.record())
        self.panel.client["client"]["limitIp"] = 2
        self.now += 61
        self.collect()
        records = self.engine.records()
        self.assertEqual(len(records), 2)
        self.assertIn(original, records)
        self.assertEqual(len({r["report"]["finding_ref"] for r in records}), 1)
        self.assertEqual(len({r["report"]["revision"] for r in records}), 2)

    def test_managed_and_infrastructure_and_hidden_membership_are_excluded(self):
        for marker in ("email", "uuid", "subId"):
            with self.subTest(marker=marker):
                self.save_access({"identity": self.panel.client["client"][marker]})
                self.assertEqual(self.collect()["state"], "IDLE")
                self.assertEqual(self.engine.records(), [])
                self.now += 61
        (self.access / "fixture.1.json").unlink()
        self.config["infrastructure"] = {"identity": self.panel.client["client"]["uuid"]}
        self.config_path.write_text(json.dumps(self.config))
        self.assertEqual(self.collect()["state"], "IDLE")
        self.now += 61
        self.config.pop("infrastructure")
        self.config_path.write_text(json.dumps(self.config))
        self.panel.client["inboundIds"] = [9, 10]
        self.assertEqual(self.collect()["state"], "IDLE")
        self.assertEqual(self.engine.records(), [])

    def test_observable_drift_produces_no_report(self):
        self.panel.drift = True
        with self.assertRaisesRegex(findings.FindingError, "OBSERVATION_DRIFT"):
            self.collect()
        self.assertEqual(self.engine.records(), [])

    def test_node_lock_and_unresolved_transaction_block_collection(self):
        with findings.node_mutation_lock(self.lock):
            with self.assertRaises(Exception):
                self.collect()
        self.assertEqual(self.panel.calls, [])
        self.now += 61
        transaction = self.root / "transactions" / "fixture"
        transaction.mkdir(parents=True)
        (transaction / "plan.json").write_text('{"schema_version":1}')
        (transaction / "result.json").write_text('{"status":"pending"}')
        with self.assertRaises(Exception):
            self.collect()
        self.assertEqual(self.panel.calls, [])

    def test_sender_holds_store_lock_but_releases_node_mutation_lock(self):
        self.collect()
        original = self.api.api_json
        def inspect(*args, **kwargs):
            with findings.node_mutation_lock(self.lock):
                pass
            with self.assertRaises(Exception):
                with findings.node_mutation_lock(self.journal / ".lock"):
                    pass
            return original(*args, **kwargs)
        self.api.api_json = inspect
        self.assertEqual(self.engine.cycle(self.api)["state"], "OBSERVED_ONLY")

    def test_corrupt_or_wrong_scope_evidence_cannot_be_sent(self):
        self.collect()
        with self.assertRaises(findings.FindingError):
            self.new_engine("other_node").cycle(self.api)
        record = self.record()
        path = self.journal / (record["report"]["revision"] + ".json")
        record["baseline"]["client_record"]["client"]["enable"] = False
        findings.write_json(path, record)
        with self.assertRaises(findings.FindingError):
            self.new_engine().cycle(self.api)
        self.assertEqual(self.api.calls, [])

    def test_private_files_reject_symlink_hardlink_fifo_and_weak_permissions(self):
        self.collect()
        record = self.record()
        path = self.journal / (record["report"]["revision"] + ".json")
        path.chmod(0o644)
        with self.assertRaises(findings.FindingError):
            self.engine.cycle(self.api)
        path.chmod(0o600)
        link = self.root / "hardlink"
        os.link(path, link)
        with self.assertRaises(findings.FindingError):
            self.engine.cycle(self.api)
        link.unlink()
        path.unlink()
        path.symlink_to(self.config_path)
        with self.assertRaises(findings.FindingError):
            self.engine.cycle(self.api)
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(findings.FindingError):
            self.engine.cycle(self.api)
        self.assertEqual(self.api.calls, [])

    def test_missing_or_unsafe_access_inventory_cannot_be_classified_empty(self):
        self.access.chmod(0o755)
        with self.assertRaises(findings.FindingError):
            self.collect()
        self.access.rmdir()
        self.now += 61
        with self.assertRaises(findings.FindingError):
            self.collect()
        self.assertEqual(self.engine.records(), [])

    def test_inventory_walk_error_is_not_silently_skipped(self):
        def failed_walk(*args, **kwargs):
            kwargs["onerror"](PermissionError("private_path_canary"))
            return iter(())
        with mock.patch.object(findings.os, "walk", side_effect=failed_walk):
            with self.assertRaisesRegex(findings.FindingError, "^ACCESS_INVENTORY_UNAVAILABLE$"):
                self.collect()
        self.assertEqual(self.engine.records(), [])

    def test_duplicate_json_keys_are_rejected_before_collection(self):
        self.config_path.write_text('{"duplicate":1,"duplicate":2}')
        with self.assertRaisesRegex(findings.FindingError, "INVALID_JSON"):
            self.collect()
        self.assertEqual(self.engine.records(), [])

    def test_journal_capacity_checked_before_writing_new_record(self):
        self.collect()
        original = self.record()
        candidate = json.loads(json.dumps(original))
        candidate["report"]["revision"] = str(uuid.uuid4())
        with mock.patch.object(findings, "MAX_RECORDS", 1):
            with self.assertRaisesRegex(findings.FindingError, "JOURNAL_LIMIT"):
                self.engine.save(candidate)
        self.assertEqual(self.engine.records(), [original])

    def test_immutable_save_rejects_changed_report_even_with_matching_new_digest(self):
        self.collect()
        record = self.record()
        record["baseline"]["nonce"] = "b" * 64
        record["report"]["baseline_sha256"] = findings.digest(record["baseline"])
        with self.assertRaisesRegex(findings.FindingError, "IMMUTABLE_EVIDENCE_CHANGED"):
            self.engine.save(record)


class AgentIntegrationTests(unittest.TestCase):
    def config(self, directory, mode="observe", auth="mtls"):
        path = Path(directory) / "agent.env"
        path.write_text("\n".join(["WAVEMESH_API_BASE=https://api.example.invalid/api", "WAVEMESH_NODE_ID=node_fixture",
            "WAVEMESH_TENANT_ID=tenant_fixture", "WAVEMESH_AGENT_AUTH_MODE=" + auth,
            "WAVEMESH_AGENT_MTLS_MODE=shadow", "WAVEMESH_AGENT_MTLS_API_BASE=https://mtls.example.invalid/api",
            "WAVEMESH_AGENT_RUNTIME_FINDINGS_MODE=" + mode,
            "WAVEMESH_AGENT_RUNTIME_PATH=" + str(Path(directory) / "runtime.json"),
            "WAVEMESH_AGENT_TOKEN=wvn_" + "a" * 40, "WAVEMESH_AGENT_TOKEN_EXPIRES_AT=2030-01-01T00:00:00Z"]))
        return agent.AgentConfig.load(path)

    def test_config_and_agent_gates_never_collect_or_send_without_primary_mtls(self):
        with tempfile.TemporaryDirectory() as directory:
            for auth in ("bearer", "bootstrap-mtls"):
                with self.assertRaises(agent.AgentError):
                    self.config(directory, auth=auth)
            with self.assertRaises(agent.AgentError):
                self.config(directory, mode="enable")
            self.config(directory)
            path = Path(directory) / "agent.env"
            with path.open("a") as output:
                output.write("\nWAVEMESH_AGENT_RUNTIME_FINDINGS_MODE=disabled\n")
            with self.assertRaises(agent.AgentError):
                agent.AgentConfig.load(path)
            config = self.config(directory, mode="disabled")
            instance = agent.NodeAgent(config, mtls_runtime=Api())
            with mock.patch.object(agent.subprocess, "run") as run:
                instance.run_runtime_finding_cycle()
                run.assert_not_called()
                config.runtime_finding_mode = "observe"
                instance.run_runtime_finding_cycle()
                run.assert_not_called()
                self.assertEqual(instance.last_finding_status["state"], "MTLS_NOT_READY")

    def test_worker_is_bounded_and_its_private_output_never_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = agent.NodeAgent(self.config(directory), mtls_runtime=Api())
            instance.last_mtls_status = {"state": "SHADOW_ACTIVE"}
            with mock.patch.object(agent.subprocess, "run", side_effect=subprocess.TimeoutExpired("synthetic", 20, output="private_canary")) as run:
                with self.assertLogs(agent.LOG, level="WARNING") as captured:
                    instance.run_runtime_finding_cycle()
                self.assertNotIn("private_canary", " ".join(captured.output))
                self.assertEqual(run.call_args.kwargs["timeout"], 20)
                self.assertIs(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
                self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
                self.assertFalse(run.call_args.kwargs.get("shell", False))
                self.assertEqual(instance.last_finding_status["state"], "COLLECTION_BLOCKED")
            fake = mock.Mock()
            fake.return_value.cycle.return_value = {"state": "OBSERVED_ONLY"}
            with mock.patch.object(agent.subprocess, "run", return_value=SimpleNamespace(returncode=0)), mock.patch.object(agent, "RuntimeFindingCycle", fake):
                instance.run_runtime_finding_cycle()
                fake.return_value.cycle.assert_called_once_with(instance.mtls_runtime)


if __name__ == "__main__":
    unittest.main()
