"""Synthetic historical journal validation; no native owner/capability proof."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/lib"))
import prepared_journal as reader
import transaction_state as journal


def completed_fixture(root):
    directory = journal.begin(root, "auto-prepared-reconcile", 123)
    state = {"mutation_epoch": "0", "sample_at": 100, "selected": "unit", "binding": "b"*64,
             "native_generation": "00000000-0000-4000-8000-000000000002", "effective_config_sha256": "a"*64}
    candidates = {"config": {"node": {"role": "entry"}},
                  "runtime": {"prepared_routes": {"route-unit": state}, "root_revision": 1},
                  "template": {"routing": {"sentinel": "template"}},
                  "process": {"routing": {"sentinel": "process"}}}
    for kind, filename in reader.ARTIFACTS.items():
        journal.atomic_json(directory / filename, candidates[kind])
    intent = {"schema_version": 1, "transaction_id": directory.name, "route_id": "route-unit", "binding": "b"*64,
              "expected": {"generation": "00000000-0000-4000-8000-000000000002", "epoch": "0",
                           "engine_instance": {"boot_id": "00000000-0000-4000-8000-000000000001", "pid": 123, "start_ticks": "100"},
                           "before_sha256": "a"*64, "after_sha256": "c"*64, "normalized_sha256": "d"*64,
                           "rule_tag": "wm-rule-unit", "inbound_tag": "wm-route-unit", "target": "wm-exit-path-unit"},
              "files": {kind: str((root.parent / kind).absolute()) for kind in ("config", "runtime", "database", "process")},
              "before": {"config": "e"*64, "runtime": None, "owned": None, "template": "f"*64, "process": "a"*64},
              "candidate": {kind: reader.sha((directory / filename).read_bytes()) for kind, filename in reader.ARTIFACTS.items()}}
    marker = directory / "prepared.apply.completed.json"
    journal.atomic_json(marker, intent)
    reply = {"schema_version": 1, "transaction_id": directory.name, "intent_sha256": reader.sha(marker.read_bytes()),
             "outcome": {key: key != "NoOp" for key in reader.OUTCOME}}
    journal.atomic_json(directory / "prepared.owner-result.json", reply)
    post = copy.deepcopy(candidates["runtime"])
    post["root_revision"] = 2
    post["foreign_root_field"] = {"preserved": True}
    journal.atomic_json(directory / "prepared.runtime.post.json", post)
    proof = {"schema_version": 1, "transaction_id": directory.name, "intent_sha256": reader.sha(marker.read_bytes()),
             "intent": intent, "owner_result_sha256": reader.sha((directory / "prepared.owner-result.json").read_bytes()),
             "selector_transition": reader.expected_transition(intent),
             "local_post": {"config": intent["candidate"]["config"], "runtime": reader.sha((directory / "prepared.runtime.post.json").read_bytes()),
                            "template": reader.digest(candidates["template"]), "process": reader.digest(candidates["process"])}}
    journal.atomic_json(directory / "result.json", {"status": "committed", "finished_at": "2026-11-17T13:00:00Z", "prepared_commit": proof})
    return directory


class PreparedJournal(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wm-journal-reader-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "transactions"

    def test_valid_history_is_read_only_and_never_promotes_samples(self):
        directory = completed_fixture(self.root)
        before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
        with mock.patch.object(os, "fsync", side_effect=AssertionError("reader wrote metadata")), \
                mock.patch.object(os, "replace", side_effect=AssertionError("reader renamed")), \
                mock.patch.object(Path, "write_bytes", side_effect=AssertionError("reader wrote")):
            self.assertEqual(reader.prepared_status(directory), "committed")
            self.assertEqual(journal.incomplete(self.root), [])
            self.assertEqual(journal.records(self.root)[0]["status"], "committed")
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()})
        state = json.loads((directory / "prepared.runtime.post.json").read_text())["prepared_routes"]["route-unit"]
        self.assertEqual(state["mutation_epoch"], "0")
        self.assertEqual(journal.incomplete(self.root), [])

    def test_pending_always_wins_and_generic_mark_keeps_full_proof(self):
        directory = completed_fixture(self.root)
        before = (directory / "result.json").read_bytes()
        journal.mark(directory, "committed")
        self.assertEqual((directory / "result.json").read_bytes(), before)
        for status in ("recovering", "rollback_failed", "rolled_back"):
            with self.assertRaises(RuntimeError):
                journal.mark(directory, status)
        journal.atomic_json(directory / "prepared.apply.pending.json", {})
        self.assertEqual(reader.prepared_status(directory), "active")
        with self.assertRaises(RuntimeError):
            journal.mark(directory, "committed")
        journal.prune(self.root, 0)
        self.assertTrue(directory.is_dir())

    def test_redirected_transaction_is_refused_before_reading_payload(self):
        with mock.patch.object(Path, "is_symlink", return_value=True), \
                mock.patch.object(reader, "read_private", side_effect=AssertionError("followed redirected journal")):
            self.assertEqual(reader.prepared_status(self.root / "redirected"), "active")

    def test_foreign_stale_duplicate_or_contradictory_proofs_stay_active(self):
        for change in ("foreign", "lost-reply", "partial-owner", "stale-epoch", "wrong-after", "wrong-intent", "boolean-copy", "duplicate-key", "missing-artifact", "changed-candidate", "changed-runtime-post", "missing-origin", "missing-origin-bad-result"):
            with self.subTest(change=change):
                root = self.root / change
                directory = completed_fixture(root)
                result = json.loads((directory / "result.json").read_text())
                if change == "foreign":
                    result["prepared_commit"]["transaction_id"] = "other"
                elif change == "lost-reply":
                    (directory / "prepared.owner-result.json").unlink()
                elif change == "partial-owner":
                    reply = json.loads((directory / "prepared.owner-result.json").read_text())
                    reply["outcome"]["ConfigPersisted"] = False
                    journal.atomic_json(directory / "prepared.owner-result.json", reply)
                    result["prepared_commit"]["owner_result_sha256"] = reader.sha((directory / "prepared.owner-result.json").read_bytes())
                elif change == "stale-epoch":
                    result["prepared_commit"]["selector_transition"]["to_epoch"] = "2"
                elif change == "wrong-after":
                    result["prepared_commit"]["selector_transition"]["after_sha256"] = "e"*64
                elif change == "wrong-intent":
                    result["prepared_commit"]["intent"]["route_id"] = "route-other"
                elif change == "boolean-copy":
                    result["prepared_commit"]["intent"]["schema_version"] = True
                elif change == "missing-artifact":
                    (directory / "prepared.process.json").unlink()
                elif change == "changed-candidate":
                    journal.atomic_json(directory / "prepared.xray.json", {"different": True})
                elif change == "changed-runtime-post":
                    journal.atomic_json(directory / "prepared.runtime.post.json", {"prepared_routes": {}})
                elif change in ("missing-origin", "missing-origin-bad-result"):
                    (directory / "prepared.apply.completed.json").unlink()
                journal.atomic_json(directory / "result.json", result)
                if change == "missing-origin-bad-result":
                    (directory / "result.json").write_bytes(b"{broken-json")
                if change == "duplicate-key":
                    data = (directory / "result.json").read_bytes()
                    (directory / "result.json").write_bytes(data.replace(b'"status": "committed"', b'"status": "bad", "status": "committed"'))
                self.assertEqual(reader.prepared_status(directory), "active")
                journal.prune(root, 0)
                self.assertTrue(directory.is_dir())

    def test_verified_terminal_admission_repeats_directory_barrier(self):
        directory = completed_fixture(self.root)
        real = journal._sync_directory
        def fail(path):
            if Path(path) == directory:
                raise OSError("synthetic failed completed directory barrier")
            real(path)
        with mock.patch.object(journal, "_sync_directory", side_effect=fail):
            for action in (lambda: journal.admit(self.root),
                           lambda: journal.begin(self.root, "must-not-start", 124),
                           lambda: journal.prune(self.root, 0),
                           lambda: journal.mark(directory, "committed")):
                with self.assertRaises(OSError):
                    action()
                self.assertTrue(directory.is_dir())
        # Only metadata is retried, never a native/business action.
        journal.mark(directory, "committed")
        journal.prune(self.root, 0)
        self.assertFalse(directory.exists())

    def test_mutating_admission_refuses_debt_before_any_durability_action(self):
        directory = completed_fixture(self.root)
        journal.atomic_json(directory / "prepared.apply.pending.json", {})
        with mock.patch.object(journal, "_sync_directory", side_effect=AssertionError("admission passed unresolved debt")):
            with self.assertRaisesRegex(RuntimeError, "incomplete transaction"):
                journal.admit(self.root)


if __name__ == "__main__":
    unittest.main()
