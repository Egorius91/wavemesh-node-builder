"""New adapter/failure contracts. Synthetic FD9/collector are NOT native proof."""
import copy
from contextlib import closing
from contextlib import contextmanager, ExitStack, nullcontext
from pathlib import Path
import sys
import sqlite3
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/lib"))
import prepared_completion as completion
import prepared_evaluation as evaluation
import prepared_execution as execution
import prepared_intent as writer
import prepared_journal as reader
import prepared_owner as owner
import prepared_reconcile as caller
import transaction_state as journal
from tests.unit import test_prepared_intent as intent_fixtures
from tests.unit import test_prepared_execution as execution_fixtures


class StableOwningContext(unittest.TestCase):
    def check_frame(self, mode):
        first = execution_fixtures.snapshot("a" * 64)
        second = execution_fixtures.snapshot("b" * 64)
        third = execution_fixtures.snapshot("c" * 64)
        peer = 10
        if mode == "epoch": third["epoch"] = "1"
        elif mode == "nonce": third["challenge"] = "b" * 64
        elif mode == "peer": peer += 1
        elif mode == "config":
            third["effective_config"]["new_unowned_value"] = True
            third["effective_config_sha256"] = reader.digest(third["effective_config"])
            third["execution_sha256"] = reader.digest({"effective_config": third["effective_config"], "balancer_overrides": {}})
        with ExitStack() as stack:
            stream = stack.enter_context(tempfile.TemporaryFile())
            stream.write(execution_fixtures.PRODUCER_BYTES)
            stream.seek(0)
            stack.enter_context(mock.patch.object(execution.sys, "platform", "linux"))
            pins = execution_fixtures.approval()
            stack.enter_context(mock.patch.object(execution, "_load_approval", return_value=pins))
            stack.enter_context(mock.patch.object(execution, "_connection", return_value=nullcontext((None, 10, None))))
            stack.enter_context(mock.patch.object(execution, "_process", return_value={"pid": 10, "start_ticks": "100", "parent_pid": 1}))
            stack.enter_context(mock.patch.object(execution, "_open", side_effect=lambda path, resources: resources.enter_context(stream)))
            frames = stack.enter_context(mock.patch.object(execution, "_frame", side_effect=[
                (first, first["challenge"], 10), (second, second["challenge"], 10), (third, third["challenge"], peer)]))
            stack.enter_context(mock.patch.object(execution, "_observe", return_value="same verified evidence"))
            checks = stack.enter_context(mock.patch.object(execution, "_recheck"))
            with execution._collected_evidence(stable=True) as (original, evidence, recheck):
                frozen = copy.deepcopy(original)
                self.assertEqual(evidence, "same verified evidence")
                self.assertFalse(stream.closed)
                if mode == "unchanged":
                    recheck()
                else:
                    with self.assertRaises(execution.ExecutionUnavailable): recheck()
                self.assertEqual(original, frozen, "must not adopt the newer context")
                self.assertFalse(stream.closed)
                self.assertEqual(frames.call_args_list, [mock.call(10)] * 3)
                self.assertGreaterEqual(checks.call_count, 2)
        self.assertTrue(stream.closed)

    def test_same_context_recheck_keeps_descriptors_and_original_full_image(self):
        self.check_frame("unchanged")

    def test_epoch_config_peer_and_replayed_challenge_cannot_rebind_old_samples(self):
        for mode in ("epoch", "config", "peer", "nonce"):
            with self.subTest(mode=mode): self.check_frame(mode)


class PreparedBootstrap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wm-prepared-bootstrap-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        patch = mock.patch.object(completion, "_require_node_owner")
        patch.start()
        self.addCleanup(patch.stop)

    def begin(self, root=None):
        return journal.begin(root or self.root, "auto-prepared-reconcile", 123, prepared=True)

    def assert_fenced(self, path):
        self.assertEqual(reader.prepared_status(path), "active")
        self.assertEqual(journal.incomplete(path.parent)[0]["id"], path.name)
        with self.assertRaises(RuntimeError): journal.admit(path.parent)
        for status in ("committed", "rolled_back", "recovering", "rollback_failed"):
            with self.assertRaises(RuntimeError): journal.mark(path, status)
        journal.prune(path.parent, 0)
        self.assertTrue(path.is_dir())

    def test_every_bootstrap_crash_is_recognized_before_path_exposure(self):
        real_atomic, real_sync = journal.atomic_json, journal._sync_directory
        for boundary in ("mkdir", "before-plan.json", "after-plan.json", "before-result.json",
                         "after-result.json", "before-" + reader.ORIGIN, "after-" + reader.ORIGIN):
            with self.subTest(boundary=boundary):
                root = self.root / boundary
                def atomic(path, value):
                    if boundary == "before-" + Path(path).name: raise OSError("injected crash")
                    real_atomic(path, value)
                    if boundary == "after-" + Path(path).name: raise OSError("injected crash")
                def sync(path):
                    if boundary == "mkdir" and Path(path) == root and list(root.glob("*" + reader.PREPARED_SUFFIX)):
                        raise OSError("injected parent barrier failure")
                    real_sync(path)
                with mock.patch.object(journal, "atomic_json", side_effect=atomic), mock.patch.object(journal, "_sync_directory", side_effect=sync):
                    with self.assertRaises(OSError): self.begin(root)
                directories = list(root.glob("*" + reader.PREPARED_SUFFIX))
                self.assertEqual(len(directories), 1)
                self.assert_fenced(directories[0])

    def test_valid_origin_alone_cannot_authorize_no_effect_or_generic_restore(self):
        path = self.begin()
        self.assertEqual({p.name for p in path.iterdir()}, {"plan.json", "result.json", reader.ORIGIN})
        self.assert_fenced(path)
        with self.assertRaises(RuntimeError): journal.abort_prepared(path)
        self.assert_fenced(path)
        self.assertTrue((path / reader.ORIGIN).exists())

    def test_pending_request_reply_completed_or_unreadable_origin_never_abort(self):
        for marker in ("prepared.apply.pending.json", "prepared.owner-request.json", "prepared.owner-result.json", "prepared.apply.completed.json", reader.ORIGIN):
            with self.subTest(marker=marker):
                path = self.begin(self.root / marker)
                journal.atomic_bytes(path / marker, b"broken")
                before = {p.name: p.read_bytes() for p in path.iterdir()}
                with self.assertRaises((ValueError, RuntimeError)): journal.abort_prepared(path)
                self.assert_fenced(path)
                self.assertEqual(before, {p.name: p.read_bytes() for p in path.iterdir()})

    def test_ordinary_and_valid_early_legacy_journals_keep_existing_behavior(self):
        for operation in ("ordinary-snapshot", "auto-prepared-reconcile", "auto-prepared-install"):
            path = journal.begin(self.root / operation, operation, 123)
            self.assertIsNone(reader.prepared_status(path))
            journal.atomic_json(path / "config.before.json", {"ordinary": True})
            journal.mark(path, "recovering")
            journal.mark(path, "rolled_back")
            self.assertEqual(journal.records(path.parent)[0]["status"], "rolled_back")
            journal.prune(path.parent, 0)
            self.assertFalse(path.exists())

    def test_missing_owner_refuses_before_reserving_a_directory(self):
        with mock.patch.object(completion, "_require_node_owner", side_effect=ValueError("no FD9")):
            with self.assertRaises(ValueError): self.begin()
        self.assertEqual(list(self.root.iterdir()), [])


class OwningCaller(unittest.TestCase):
    def setUp(self):
        self.f = intent_fixtures.PreparedIntent()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(completion, "_require_node_owner"))
        self.directory = journal.begin(self.f.base / "owning", "auto-prepared-reconcile", 123, prepared=True)
        self.active_context = False
        self.collected = 0
        self.rechecked = 0
        self.during_collect = lambda: None
        self.during_recheck = lambda: None
        self.observed = object()

        @contextmanager
        def collected(*, stable):
            self.assertIs(stable, True)
            self.collected += 1
            self.assertFalse(self.active_context)
            self.active_context = True
            try:
                self.during_collect()
                yield ((copy.deepcopy(self.f.case.view), copy.deepcopy(self.f.effective)), self.observed,
                       lambda: recheck(self.observed))
            finally:
                self.active_context = False
        def recheck(observed):
            self.assertIs(observed, self.observed)
            self.assertTrue(self.active_context, "verified identity descriptors closed before persistence")
            self.rechecked += 1
            self.during_recheck()
        self.stack.enter_context(mock.patch.object(execution, "_collected_evidence", side_effect=collected))
        self.stack.enter_context(mock.patch.object(execution, "_recheck", side_effect=recheck))
        self.stack.enter_context(mock.patch.object(execution, "collect", side_effect=AssertionError("second independent collection")))
        self.stack.enter_context(mock.patch.object(evaluation.time, "time", return_value=110))
        self.send = self.stack.enter_context(mock.patch.object(owner, "apply_once", side_effect=AssertionError("unexpected owner send")))
        for target in ("socket.socket.connect", "http.client.HTTPConnection.request", "subprocess.Popen"):
            self.stack.enter_context(mock.patch(target, side_effect=AssertionError("unexpected external side effect")))

    def run_caller(self):
        return caller.reconcile(self.directory, self.f.case.route_id, self.f.files,
                                self.f.case.observations, self.f.base / "existing-private-bearer.json")

    def noop(self):
        self.f.case.observations["paths"] = dict.fromkeys(self.f.case.observations["paths"], False)

    def assert_debt(self):
        self.assertEqual(reader.prepared_status(self.directory), "active")
        with self.assertRaises(RuntimeError): journal.admit(self.directory.parent)
        with self.assertRaises((ValueError, RuntimeError, KeyError)): journal.abort_prepared(self.directory)
        journal.prune(self.directory.parent, 0)
        self.assertTrue(self.directory.is_dir())

    def test_noop_uses_one_open_context_and_changes_only_health_with_history_readback(self):
        self.noop()
        self.assertEqual(self.run_caller(), "health-only")
        self.assertEqual(self.collected, 1)
        self.assertGreater(self.rechecked, 0)
        self.assertFalse(self.active_context)
        self.send.assert_not_called()
        for key in ("config", "database", "process"):
            self.assertEqual(Path(self.f.files[key]).read_bytes(), self.f.before[key])
        runtime = reader.decode(Path(self.f.files["runtime"]).read_bytes())
        self.assertEqual(runtime["unowned"], {"keep": True})
        proof = (self.directory / "result.json").read_bytes()
        self.assertEqual(reader.prepared_status(self.directory), "committed")
        self.assertFalse(journal.incomplete(self.directory.parent))
        journal.mark(self.directory, "committed")
        self.assertEqual(self.run_caller(), "historical-committed")
        self.assertEqual(completion.complete(self.directory), "committed")
        self.assertEqual(proof, (self.directory / "result.json").read_bytes())
        self.assertEqual(self.collected, 1)
        self.send.assert_not_called()

    def test_update_freezes_once_then_calls_one_owner_and_never_replays_unknown(self):
        def pending(directory, credential_file):
            reader.validate_intent(reader.decode(reader.read_private(directory / "prepared.apply.pending.json")), directory.name)
            journal.atomic_json(directory / "prepared.owner-request.json", {"synthetic": "one dispatched request"})
            return "pending"
        self.send.side_effect = pending
        with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.assertEqual(self.collected, 1)
        self.send.assert_called_once()
        self.assert_debt()
        original = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.send.assert_called_once()
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.directory.iterdir()})
        self.f.assert_storage_unchanged()

    def test_known_update_uses_real_completion_proof_then_history_never_sends_again(self):
        def known(directory, _):
            self.assertFalse(self.active_context)
            raw = reader.read_private(directory / "prepared.apply.pending.json")
            intent = reader.validate_intent(reader.decode(raw), directory.name)
            journal.atomic_json(directory / "prepared.owner-request.json", {"synthetic": "one owner call"})
            template = reader.decode(reader.read_private(directory / "prepared.xray.json"))
            import json
            with closing(sqlite3.connect(self.f.files["database"])) as db:
                db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(template), "xrayTemplateConfig"))
                db.commit()
            journal.atomic_json(self.f.files["process"], template)
            reply = {"schema_version": 1, "transaction_id": directory.name, "intent_sha256": reader.sha(raw),
                     "outcome": {**{key: key != "NoOp" for key in reader.OUTCOME},
                                 "ProcessConfigSHA256": reader.sha(Path(self.f.files["process"]).read_bytes())}}
            after = copy.deepcopy(self.f.effective)
            next(rule for rule in after["routing"]["rules"] if rule.get("ruleTag") == intent["expected"]["rule_tag"])["outboundTag"] = intent["expected"]["target"]
            view = copy.deepcopy(self.f.case.view)
            view.update(mutation_epoch="1", effective_config_sha256=reader.digest(after),
                        selector_transitions=[reader.expected_transition(intent)])
            self.post_context = copy.deepcopy((view, after))
            with mock.patch.object(execution, "collect", return_value=(view, after)) as post_collect:
                completion.record_owner_result(directory, reply)
                result = completion.complete(directory)
                self.assertGreaterEqual(post_collect.call_count, 2)
                return result
        self.send.side_effect = known
        self.assertEqual(self.run_caller(), "committed")
        self.send.assert_called_once()
        self.assertEqual(reader.prepared_status(self.directory), "committed")
        self.assertFalse(journal.incomplete(self.directory.parent))
        proof = (self.directory / "result.json").read_bytes()
        self.assertEqual(self.run_caller(), "historical-committed")
        journal.mark(self.directory, "committed")
        self.assertEqual(proof, (self.directory / "result.json").read_bytes())
        self.send.assert_called_once()
        self.assertEqual(self.collected, 1)
        # A subsequent routine tick uses current epoch1 evidence, keeps the
        # accepted selector and changes health only; history is not relabelled.
        self.f.case.view, self.f.effective = self.post_context
        observation = self.f.case.observations
        observation.update(at=120, effective_config_sha256=self.f.case.view["effective_config_sha256"],
                           mutation_epoch_before="1", mutation_epoch_after="1")
        evaluation.time.time.return_value = 120
        self.directory = journal.begin(self.directory.parent, "auto-prepared-reconcile", 123, prepared=True)
        self.assertEqual(self.run_caller(), "health-only")
        self.send.assert_called_once()
        self.assertEqual(self.collected, 2)

    def test_stale_foreign_context_and_preimage_drift_stop_before_pending(self):
        self.noop()
        self.f.case.observations["engine_instance_after"]["pid"] += 1
        with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.assertFalse((self.directory / "prepared.apply.pending.json").exists())
        self.send.assert_not_called()
        self.f.assert_storage_unchanged()
        self.assert_debt()

    def test_stale_sample_stops_without_health_write(self):
        self.noop()
        self.f.case.observations["at"] = 1
        with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.send.assert_not_called()
        self.f.assert_storage_unchanged()
        self.assert_debt()

    def test_preimage_drift_during_collection_preserves_foreign_value(self):
        self.noop()
        self.during_collect = lambda: journal.atomic_json(self.f.files["runtime"], {"foreign": True})
        with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.assertEqual(reader.decode(Path(self.f.files["runtime"]).read_bytes()), {"foreign": True})
        self.assertFalse((self.directory / "prepared.apply.pending.json").exists())
        self.send.assert_not_called()
        self.assert_debt()

    def test_foreign_process_after_pending_retains_debt_without_health_write(self):
        self.noop()
        def changed():
            if (self.directory / "prepared.apply.pending.json").exists():
                raise execution.ExecutionUnavailable("synthetic foreign process")
        self.during_recheck = changed
        with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.assert_debt()
        self.send.assert_not_called()
        self.f.assert_storage_unchanged()

    def test_partial_health_write_keeps_debt_and_cannot_be_repeated_or_aborted(self):
        self.noop()
        atomic = journal.atomic_bytes
        def failed(path, data):
            atomic(path, data)
            if Path(path) == Path(self.f.files["runtime"]): raise OSError("lost persistence barrier")
        with mock.patch.object(journal, "atomic_bytes", side_effect=failed):
            with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        changed = Path(self.f.files["runtime"]).read_bytes()
        self.assertNotEqual(changed, self.f.before["runtime"])
        self.assert_debt()
        with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.assertEqual(changed, Path(self.f.files["runtime"]).read_bytes())
        self.send.assert_not_called()

    def test_visible_pending_barrier_failure_never_calls_owner_or_mutates_storage(self):
        real = journal._sync_directory
        def failed(path):
            if Path(path) == self.directory and (self.directory / "prepared.apply.pending.json").exists():
                raise OSError("injected pending directory barrier")
            real(path)
        with mock.patch.object(journal, "_sync_directory", side_effect=failed):
            with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.assert_debt()
        self.send.assert_not_called()
        self.f.assert_storage_unchanged()

    def test_changed_frozen_health_artifact_stops_before_live_write(self):
        self.noop()
        real = journal.atomic_json
        def drift(path, value):
            real(path, value)
            if Path(path).name == "prepared.apply.pending.json":
                real(self.directory / "prepared.runtime.json", {"foreign": True})
        with mock.patch.object(journal, "atomic_json", side_effect=drift):
            with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.assert_debt()
        self.f.assert_storage_unchanged()
        self.send.assert_not_called()

    def test_health_terminal_write_failure_keeps_pending_and_cannot_claim_success(self):
        self.noop()
        real = journal.atomic_json
        def failed(path, value):
            real(path, value)
            if Path(path).name == "result.json" and value.get("status") == "committed":
                raise OSError("injected result barrier")
        with mock.patch.object(journal, "atomic_json", side_effect=failed):
            with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.assert_debt()
        self.send.assert_not_called()
        with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()

    def test_failed_visible_completion_barrier_requires_durability_repair_before_new_writer(self):
        self.noop()
        real = journal._sync_directory
        def failed(path):
            if Path(path) == self.directory and (self.directory / "prepared.apply.completed.json").exists():
                raise OSError("injected completed directory barrier")
            real(path)
        with mock.patch.object(journal, "_sync_directory", side_effect=failed):
            with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
            with self.assertRaises(OSError): journal.admit(self.directory.parent)
        # Visible, fully valid historical facts may be resynced, never rewritten.
        proof = (self.directory / "result.json").read_bytes()
        journal.admit(self.directory.parent)
        self.assertEqual(self.run_caller(), "historical-committed")
        self.assertEqual(proof, (self.directory / "result.json").read_bytes())
        self.send.assert_not_called()

    def test_expired_sample_after_pending_never_relabels_health_as_success(self):
        self.noop()
        real = journal.atomic_json
        def expired(path, value):
            real(path, value)
            if Path(path).name == "prepared.apply.pending.json": evaluation.time.time.return_value = 151
        with mock.patch.object(journal, "atomic_json", side_effect=expired):
            with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.assert_debt()
        self.send.assert_not_called()
        self.f.assert_storage_unchanged()

    def test_health_kind_cannot_enter_update_parser_or_one_send_owner(self):
        self.noop()
        self.assertEqual(self.run_caller(), "health-only")
        raw = reader.decode(reader.read_private(self.directory / "prepared.apply.completed.json"))
        with self.assertRaises(ValueError): reader.validate_intent(raw, self.directory.name)
        self.send.assert_not_called()

    def test_lost_or_damaged_health_completion_proof_never_becomes_no_effect(self):
        self.noop()
        self.assertEqual(self.run_caller(), "health-only")
        (self.directory / "prepared.apply.completed.json").unlink()
        self.assert_debt()
        with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.send.assert_not_called()

    def test_missing_collector_never_claims_installed_success(self):
        self.noop()
        with mock.patch.object(execution, "_collected_evidence", side_effect=execution.ExecutionUnavailable("not installed")):
            with self.assertRaisesRegex(caller.ReconcileUnavailable, "^PREPARED_RECONCILE_NOT_VERIFIED_RETAIN_JOURNAL$"):
                self.run_caller()
        self.send.assert_not_called()
        self.f.assert_storage_unchanged()
        self.assert_debt()

    def stop_pre_effect(self):
        real = journal.atomic_json
        def stopped(path, value):
            real(path, value)
            if Path(path).name == "plan.json" and value.get("prepared_phase") == "pre-effect":
                raise OSError("crash before effect-possible phase")
        with mock.patch.object(journal, "atomic_json", side_effect=stopped):
            with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.assertFalse((self.directory / "prepared.apply.pending.json").exists())

    def test_verified_pre_effect_closes_only_after_fresh_identity_and_live_preimage_readback(self):
        self.noop()
        self.stop_pre_effect()
        self.assertEqual(reader.prepared_status(self.directory), "active")
        journal.abort_prepared(self.directory)
        self.assertEqual(reader.prepared_status(self.directory), "rolled_back")
        self.assertFalse(journal.incomplete(self.directory.parent))
        proof = (self.directory / "result.json").read_bytes()
        journal.abort_prepared(self.directory)
        journal.mark(self.directory, "rolled_back")
        self.assertEqual(proof, (self.directory / "result.json").read_bytes())
        self.send.assert_not_called()
        self.f.assert_storage_unchanged()

    def test_pre_effect_with_changed_live_runtime_cannot_close(self):
        self.noop()
        self.stop_pre_effect()
        journal.atomic_json(self.f.files["runtime"], {"foreign": True})
        self.assert_debt()
        self.assertEqual(reader.decode(Path(self.f.files["runtime"]).read_bytes()), {"foreign": True})

    def test_lost_pending_and_owner_facts_cannot_recreate_pre_effect_or_new_writer(self):
        self.send.side_effect = lambda directory, _: "pending"
        with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        # Deliberately damage synthetic retained facts; NEVER product cleanup.
        (self.directory / "prepared.apply.pending.json").unlink()
        self.assert_debt()
        self.send.reset_mock()
        with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.send.assert_not_called()

    def test_lost_all_preparation_files_cannot_reset_durable_effect_phase(self):
        self.send.side_effect = lambda directory, _: "pending"
        with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        for path in self.directory.iterdir():
            if path.name not in ("plan.json", "result.json", reader.ORIGIN): path.unlink()
        self.assert_debt()
        plan_before = (self.directory / "plan.json").read_bytes()
        self.send.reset_mock()
        with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        self.send.assert_not_called()
        self.assertEqual(plan_before, (self.directory / "plan.json").read_bytes())

    def test_lost_health_pending_after_live_write_cannot_be_false_no_effect(self):
        self.noop()
        atomic = journal.atomic_bytes
        def stopped(path, data):
            atomic(path, data)
            if Path(path) == Path(self.f.files["runtime"]): raise OSError("crash after health write")
        with mock.patch.object(journal, "atomic_bytes", side_effect=stopped):
            with self.assertRaises(caller.ReconcileUnavailable): self.run_caller()
        (self.directory / "prepared.apply.pending.json").unlink()
        self.assert_debt()
        self.send.assert_not_called()

    def test_corrupt_or_lost_phase_facts_remain_debt_on_valid_origin(self):
        self.stop_pre_effect()
        plan = reader.decode(reader.read_private(self.directory / "plan.json"))
        plan.pop("prepared_phase")
        journal.atomic_json(self.directory / "plan.json", plan)
        self.assert_debt()


if __name__ == "__main__":
    unittest.main()
