"""Private writer contracts. Portable collector/FD9 fixtures are not native proof."""
import copy
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/lib"))
import prepared_channels as channels
import prepared_completion as completion
import prepared_evaluation as evaluation
import prepared_execution as execution
import prepared_intent as writer
import prepared_journal as reader
import transaction_state as journal
from tests.unit import test_prepared_evaluation as evaluation_tests


class PreparedIntent(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wm-intent-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        case = evaluation_tests.PreparedEvaluation()
        case.setUp()
        self.case = case
        case.config["private_sentinel"] = "узел / café"
        case.runtime["unowned"] = {"keep": True}
        self.effective = copy.deepcopy(case.template)
        self.effective["generated_sentinel"] = "actual-only"
        case.view["effective_config_sha256"] = channels.digest(self.effective)
        case.observations["effective_config_sha256"] = case.view["effective_config_sha256"]
        fingerprint = channels.health_config_fingerprint(case.config, case.route_id, case.plan, self.effective, case.view)
        case.runtime["prepared_routes"][case.route_id] = channels.select_paths(
            case.plan, case.binding, {}, case.observations, 100, case.view, fingerprint, "wm-rule-auto-europe")
        case.observations["at"] = 110
        self.files = {key: str(self.base / (key + (".db" if key == "database" else ".json")))
                      for key in ("config", "runtime", "database", "process")}
        for key, value in (("config", case.config), ("runtime", case.runtime), ("process", case.template)):
            journal.atomic_json(self.files[key], value)
        self.template_raw = json.dumps(case.template, ensure_ascii=False, indent="\t") + "\n"
        with closing(sqlite3.connect(self.files["database"])) as db:
            db.execute("CREATE TABLE settings (key TEXT, value TEXT)")
            db.execute("INSERT INTO settings VALUES (?,?)", ("xrayTemplateConfig", self.template_raw))
            db.commit()
        Path(self.files["database"]).chmod(0o600)
        self.before = {key: Path(path).read_bytes() for key, path in self.files.items()}
        self.directory = journal.begin(self.base / "transactions", "auto-prepared-reconcile", 123)

    def prepare(self, *, real_owner=False):
        with ExitStack() as stack:
            if not real_owner:
                stack.enter_context(mock.patch.object(completion, "_require_node_owner"))
            collect = stack.enter_context(mock.patch.object(execution, "collect", return_value=(self.case.view, self.effective)))
            stack.enter_context(mock.patch.object(evaluation.time, "time", return_value=110))
            for target in ("socket.socket.connect", "http.client.HTTPConnection.request", "subprocess.Popen"):
                stack.enter_context(mock.patch(target, side_effect=AssertionError("writer dispatched")))
            result = writer.prepare_once(self.directory, self.case.route_id, self.files, self.case.observations)
            collect.assert_called_once_with()
            return result

    def assert_storage_unchanged(self):
        self.assertEqual(self.before, {key: Path(path).read_bytes() for key, path in self.files.items()})

    def test_one_actual_context_freezes_valid_intent_without_apply(self):
        intent = self.prepare()
        self.assertEqual(reader.validate_intent(intent, self.directory.name), intent)
        self.assertEqual(intent["expected"]["before_sha256"], channels.digest(self.effective))
        after = copy.deepcopy(self.effective)
        next(r for r in after["routing"]["rules"] if r.get("ruleTag") == "wm-rule-auto-europe")["outboundTag"] = intent["expected"]["target"]
        self.assertEqual(intent["expected"]["after_sha256"], channels.digest(after))
        self.assertEqual(intent["before"]["template"], reader.sha(self.template_raw.encode()))
        self.assertEqual((self.directory / "config.before.json").read_bytes(), self.before["config"])
        self.assertEqual((self.directory / "prepared.template.before.json").read_bytes(), self.template_raw.encode())
        for kind, filename in reader.ARTIFACTS.items():
            self.assertEqual(intent["candidate"][kind], reader.sha((self.directory / filename).read_bytes()))
        self.assertEqual(reader.decode((self.directory / "result.json").read_bytes())["status"], "in_progress")
        self.assertFalse((self.directory / "prepared.owner-request.json").exists())
        self.assert_storage_unchanged()

    def test_missing_owner_refuses_before_collection_or_write(self):
        with mock.patch.object(completion, "_require_node_owner", side_effect=ValueError("no owner")):
            with self.assertRaisesRegex(writer.IntentUnavailable, "^PREPARED_INTENT_NOT_FROZEN$"):
                self.prepare(real_owner=True)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"plan.json", "result.json"})
        self.assert_storage_unchanged()

    def test_existing_legacy_dispatch_reply_completion_and_partial_preparation_refuse(self):
        for name in ("prepared.apply.pending.json", "prepared.owner-request.json", "prepared.owner-result.json",
                     "prepared.apply.completed.json", "prepared.xray.json", "config.before.json"):
            with self.subTest(name=name):
                marker = self.directory / name
                marker.write_bytes(b'{"status":"UNKNOWN"}')
                with self.assertRaises(writer.IntentUnavailable): self.prepare()
                self.assertEqual(marker.read_bytes(), b'{"status":"UNKNOWN"}')
                marker.unlink()  # Synthetic fixture only; never a product retry.
        self.assert_storage_unchanged()

    def test_unqualified_stale_and_published_refuse_without_artifacts(self):
        for mode in ("hash", "epoch", "stale", "published"):
            with self.subTest(mode=mode):
                original = copy.deepcopy((self.case.view, self.case.observations))
                if mode == "hash": self.case.view["effective_config_sha256"] = "f" * 64
                elif mode == "epoch": self.case.view["mutation_epoch"] = "2"
                elif mode == "stale": self.case.observations["at"] = 1
                else:
                    config = copy.deepcopy(self.case.config)
                    config["routes"][0]["presentation"]["published"] = True
                    journal.atomic_json(self.files["config"], config)
                with self.assertRaises(writer.IntentUnavailable): self.prepare()
                self.assertEqual({p.name for p in self.directory.iterdir()}, {"plan.json", "result.json"})
                self.case.view, self.case.observations = original
                Path(self.files["config"]).write_bytes(self.before["config"])

    def test_preimage_drift_after_collection_refuses_before_pending(self):
        real = evaluation._evaluate_collected
        def drift(*args):
            result = real(*args)
            journal.atomic_json(self.files["runtime"], {"foreign": True})
            return result
        with mock.patch.object(evaluation, "_evaluate_collected", side_effect=drift):
            with self.assertRaises(writer.IntentUnavailable): self.prepare()
        self.assertFalse((self.directory / "prepared.apply.pending.json").exists())
        self.assertEqual(reader.decode(Path(self.files["runtime"]).read_bytes()), {"foreign": True})

    def test_failed_candidate_and_pending_directory_barriers_keep_facts_without_success(self):
        original_directory = self.directory
        for marker in ("prepared.config.json", "prepared.apply.pending.json"):
            with self.subTest(marker=marker):
                # Separate fresh fixture transaction; no retry of a failed origin.
                self.directory = journal.begin(self.base / ("fault-" + marker), "auto-prepared-reconcile", 123)
                real = journal._sync_directory
                def failure(path):
                    if Path(path) == self.directory and (self.directory / marker).exists():
                        raise OSError("synthetic directory barrier failure")
                    real(path)
                with mock.patch.object(journal, "_sync_directory", side_effect=failure):
                    with self.assertRaises(writer.IntentUnavailable): self.prepare()
                self.assertTrue((self.directory / marker).exists())
                self.assertFalse((self.directory / "prepared.owner-request.json").exists())
                with self.assertRaises(writer.IntentUnavailable): self.prepare()
                self.assert_storage_unchanged()
        self.directory = original_directory

    def test_other_unknown_transaction_blocks_new_intent(self):
        other = self.directory.parent / "foreign-debt"
        other.mkdir(mode=0o700)
        journal.atomic_json(other / "prepared.owner-request.json", {"unknown": True})
        with self.assertRaises(writer.IntentUnavailable): self.prepare()
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"plan.json", "result.json"})

    def test_noop_and_aliased_paths_refuse_without_artifacts(self):
        for mode in ("noop", "alias"):
            with self.subTest(mode=mode):
                original = copy.deepcopy((self.files, self.case.observations))
                if mode == "noop": self.case.observations["paths"] = dict.fromkeys(self.case.observations["paths"], False)
                else: self.files["process"] = self.files["config"]
                with self.assertRaises(writer.IntentUnavailable): self.prepare()
                self.assertEqual({p.name for p in self.directory.iterdir()}, {"plan.json", "result.json"})
                self.files, self.case.observations = original
        self.assert_storage_unchanged()

    def test_sample_expiring_during_durable_preparation_never_returns_success(self):
        real = journal.atomic_json
        def expired(path, value):
            real(path, value)
            if Path(path).name == "prepared.runtime.json":
                evaluation.time.time.return_value = 151
        with mock.patch.object(journal, "atomic_json", side_effect=expired):
            with self.assertRaises(writer.IntentUnavailable): self.prepare()
        self.assertTrue((self.directory / "prepared.runtime.json").exists())
        self.assertFalse((self.directory / "prepared.apply.pending.json").exists())
        self.assert_storage_unchanged()


if __name__ == "__main__":
    unittest.main()
