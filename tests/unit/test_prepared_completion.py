"""Trusted-adapter receipt fixtures; no real owner transport/native acceptance."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/lib"))
import prepared_completion as completion
import prepared_journal as reader
import transaction_state as journal
from tests.unit.test_prepared_journal import completed_fixture


class PreparedCompletionRetention(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wm-owner-reply-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "transactions"
        self.directory = completed_fixture(self.root)
        (self.directory / "prepared.apply.completed.json").rename(self.directory / "prepared.apply.pending.json")
        (self.directory / "prepared.owner-result.json").unlink()
        journal.atomic_json(self.directory / "result.json", {"status": "in_progress"})
        self.reply = {"schema_version": 1, "transaction_id": self.directory.name,
                      "intent_sha256": reader.sha((self.directory / "prepared.apply.pending.json").read_bytes()),
                      "outcome": {key: key != "NoOp" for key in reader.OUTCOME}}

    def test_known_partial_and_noop_reply_retention_never_settles_debt(self):
        for kind in ("known", "partial", "no-op"):
            with self.subTest(kind=kind):
                reply = copy.deepcopy(self.reply)
                if kind == "partial":
                    reply["outcome"]["KnownCommit"] = False
                    reply["outcome"]["ConfigPersisted"] = False
                elif kind == "no-op":
                    reply["outcome"] = {key: key == "NoOp" for key in reader.OUTCOME}
                path = self.directory / "prepared.owner-result.json"
                path.unlink(missing_ok=True)  # Only this synthetic fixture is reset.
                before = (self.directory / "result.json").read_bytes()
                retained = completion.record_owner_result(self.directory, reply)
                self.assertEqual(retained, reader.sha(path.read_bytes()))
                self.assertEqual(reader.decode(path.read_bytes()), reply)
                self.assertEqual((self.directory / "result.json").read_bytes(), before)
                self.assertEqual(reader.prepared_status(self.directory), "active")
                self.assertEqual([item["id"] for item in journal.incomplete(self.root)], [self.directory.name])
                with self.assertRaises(RuntimeError):
                    journal.mark(self.directory, "committed")

    def test_foreign_context_and_verified_flag_are_refused_without_writes(self):
        for change in ("transaction", "intent", "verified", "bool-version", "string-outcome"):
            with self.subTest(change=change):
                reply = copy.deepcopy(self.reply)
                if change == "transaction": reply["transaction_id"] = "foreign"
                elif change == "intent": reply["intent_sha256"] = "f"*64
                elif change == "verified": reply["verified"] = True
                elif change == "bool-version": reply["schema_version"] = True
                elif change == "string-outcome": reply["outcome"]["KnownCommit"] = "true"
                with mock.patch.object(journal, "atomic_json", side_effect=AssertionError("foreign result wrote a file")):
                    with self.assertRaisesRegex(completion.CompletionUnavailable, "^OWNER_RESULT_NOT_RETAINED$"):
                        completion.record_owner_result(self.directory, reply)
                self.assertFalse((self.directory / "prepared.owner-result.json").exists())

    def test_duplicate_repeats_only_metadata_and_conflict_keeps_original(self):
        completion.record_owner_result(self.directory, self.reply)
        path = self.directory / "prepared.owner-result.json"
        before = path.read_bytes()
        with mock.patch.object(journal, "atomic_json", side_effect=AssertionError("duplicate rewrote evidence")):
            completion.record_owner_result(self.directory, copy.deepcopy(self.reply))
        other = copy.deepcopy(self.reply)
        other["outcome"]["KnownCommit"] = False
        with self.assertRaises(completion.CompletionUnavailable):
            completion.record_owner_result(self.directory, other)
        self.assertEqual(path.read_bytes(), before)

    def test_failed_reply_directory_barrier_retains_facts_for_metadata_retry(self):
        path = self.directory / "prepared.owner-result.json"
        real = journal._sync_directory
        def failed(directory):
            if Path(directory) == self.directory and path.exists():
                raise OSError("synthetic retained-reply barrier failed")
            real(directory)
        with mock.patch.object(journal, "_sync_directory", side_effect=failed):
            with self.assertRaises(completion.CompletionUnavailable):
                completion.record_owner_result(self.directory, self.reply)
        self.assertTrue(path.is_file())
        before = path.read_bytes()
        completion.record_owner_result(self.directory, self.reply)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(reader.prepared_status(self.directory), "active")

    def test_missing_or_completed_origin_cannot_be_replaced_by_a_reply(self):
        marker = self.directory / "prepared.apply.pending.json"
        marker.rename(self.directory / "prepared.apply.completed.json")
        with self.assertRaises(completion.CompletionUnavailable):
            completion.record_owner_result(self.directory, self.reply)
        self.assertFalse((self.directory / "prepared.owner-result.json").exists())


if __name__ == "__main__":
    unittest.main()
