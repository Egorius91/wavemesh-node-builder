"""Retain prepared UNKNOWN across restart, corrupt metadata and generic cleanup.

POSIX durability checks run only on native Linux, never as Windows proof.
"""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


TOOL = Path(__file__).resolve().parents[2] / "scripts/lib/transaction_state.py"
sys.path.insert(0, str(TOOL.parent))


def load_journal():
    spec = importlib.util.spec_from_file_location("prepared_debt_journal", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PreparedDebt(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wm-prepared-debt-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "state/transactions"
        self.journal = load_journal()

    def pending(self):
        transaction = self.journal.begin(self.root, "auto-prepared-reconcile", 123)
        self.journal.atomic_json(transaction / "prepared.apply.pending.json", {"status": "UNKNOWN"})
        return transaction

    def assert_retained(self, transaction):
        # Reimport to read only persisted files, with no in-memory receipt.
        journal = load_journal()
        records = journal.incomplete(self.root)
        self.assertEqual([record["id"] for record in records], [transaction.name])
        self.assertEqual(records[0]["status"], "rollback_failed")
        self.assertEqual(journal.resolve(self.root, transaction.name, active_only=True), transaction)
        with self.assertRaisesRegex(RuntimeError, "incomplete transaction"):
            journal.begin(self.root, "must-not-dispatch-again", 124)
        journal.prune(self.root, 0)
        self.assertTrue(os.path.lexists(transaction / "prepared.apply.pending.json"))

    def test_generic_terminal_mark_cannot_settle_a_pending_dispatch(self):
        transaction = self.pending()
        before = (transaction / "result.json").read_bytes()
        for status in ("committed", "rolled_back"):
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, "UNKNOWN"):
                self.journal.mark(transaction, status, "HTTP success or a fresh no-op is insufficient")
            self.assertEqual((transaction / "result.json").read_bytes(), before)
        self.journal.mark(transaction, "recovering")
        self.assert_retained(transaction)

    def test_known_result_before_completed_rename_survives_generic_mark(self):
        transaction = self.pending()
        self.journal.atomic_json(transaction / "result.json", {
            "status": "committed", "prepared_commit": {"malformed": "must-retain"}})
        before = (transaction / "result.json").read_bytes()
        for status in self.journal.ACTIVE | self.journal.TERMINAL:
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                self.journal.mark(transaction, status)
            self.assertEqual((transaction / "result.json").read_bytes(), before)
        self.assert_retained(transaction)

    def test_completed_origin_and_damaged_result_are_active_and_not_prunable(self):
        transaction = self.journal.begin(self.root, "auto-prepared-reconcile", 123)
        marker = transaction / "prepared.apply.completed.json"
        for intent in ("{", "null", "{}"):
            for result in (None, "{", "null", '{"status":"committed"}',
                           '{"status":"committed","prepared_commit":{}}'):
                with self.subTest(intent=intent, result=result):
                    marker.write_text(intent, encoding="utf-8")
                    outcome = transaction / "result.json"
                    if result is None:
                        outcome.unlink(missing_ok=True)
                    else:
                        outcome.write_text(result, encoding="utf-8")
                    saved = outcome.read_bytes() if outcome.exists() else None
                    self.assertEqual([item["id"] for item in self.journal.incomplete(self.root)], [transaction.name])
                    self.assertEqual(self.journal.resolve(self.root, transaction.name, True), transaction)
                    for status in self.journal.ACTIVE | self.journal.TERMINAL:
                        with self.assertRaises(RuntimeError):
                            self.journal.mark(transaction, status)
                    self.assertEqual(outcome.read_bytes() if outcome.exists() else None, saved)
                    self.journal.prune(self.root, 0)
                    self.assertTrue(os.path.lexists(marker))

    def test_completed_origin_without_plan_or_result_is_not_hidden(self):
        transaction = self.journal.begin(self.root, "auto-prepared-reconcile", 123)
        (transaction / "plan.json").unlink()
        (transaction / "result.json").unlink()
        self.journal.atomic_json(transaction / "prepared.apply.completed.json", {})
        self.assertEqual([item["id"] for item in self.journal.incomplete(self.root)], [transaction.name])
        self.assertEqual(self.journal.resolve(self.root, transaction.name, True), transaction)
        self.journal.prune(self.root, 0)
        self.assertTrue(transaction.is_dir())

    def test_terminal_result_cannot_hide_or_prune_pending_after_restart(self):
        transaction = self.pending()
        for status in ("committed", "rolled_back"):
            with self.subTest(status=status):
                self.journal.atomic_json(transaction / "result.json", {"status": status})
                self.assert_retained(transaction)

    def test_missing_corrupt_or_non_object_metadata_cannot_hide_pending(self):
        transaction = self.pending()
        for plan in (None, "{", "null", "[]"):
            for result in (None, "{", "null", "[]", '{"status":"committed"}'):
                with self.subTest(plan=plan, result=result):
                    for name, text in (("plan.json", plan), ("result.json", result)):
                        path = transaction / name
                        if text is None:
                            path.unlink(missing_ok=True)
                        else:
                            path.write_text(text, encoding="utf-8")
                    self.assert_retained(transaction)

    def test_malformed_pending_is_still_debt(self):
        transaction = self.pending()
        marker = transaction / "prepared.apply.pending.json"
        for text in ("", "{", "null", '{"status":"SUCCEEDED"}'):
            with self.subTest(marker=text):
                marker.write_text(text, encoding="utf-8")
                self.assert_retained(transaction)

    @unittest.skipUnless(sys.platform == "linux", "native Linux symlink required")
    def test_dangling_pending_symlink_remains_debt_without_reading_target(self):
        transaction = self.pending()
        marker = transaction / "prepared.apply.pending.json"
        marker.unlink()
        marker.symlink_to(transaction / "missing-target")
        self.assertFalse(marker.exists())
        self.assert_retained(transaction)
        with self.assertRaisesRegex(RuntimeError, "UNKNOWN"):
            self.journal.mark(transaction, "committed")

    def test_cli_check_reports_pending_even_without_plan_or_with_old_terminal(self):
        transaction = self.pending()
        (transaction / "plan.json").unlink()
        self.journal.atomic_json(transaction / "result.json", {"status": "committed"})
        result = subprocess.run([sys.executable, "-B", str(TOOL), "check", "--root", str(self.root)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout.strip(), transaction.name)

    def test_unknown_result_status_without_pending_is_not_an_admission(self):
        transaction = self.journal.begin(self.root, "interrupted", 123)
        self.journal.atomic_json(transaction / "result.json", {"status": "future_status"})
        with self.assertRaisesRegex(RuntimeError, "incomplete transaction"):
            self.journal.begin(self.root, "must-not-dispatch", 124)
        self.journal.prune(self.root, 0)
        self.assertTrue(transaction.exists())

    def test_ordinary_terminal_and_no_effect_transactions_keep_existing_behavior(self):
        for status in ("committed", "rolled_back"):
            transaction = self.journal.begin(self.root, "ordinary", 123)
            self.journal.atomic_json(transaction / "prepared.no-effect.json", {"status": "NO_CONFIG_MUTATION_DISPATCHED"})
            self.journal.mark(transaction, status)
            self.assertFalse(self.journal.incomplete(self.root))
            self.journal.prune(self.root, 0)
            self.assertFalse(transaction.exists())

    def test_failed_parent_barrier_is_repeated_before_next_begin_can_return(self):
        events = []
        real_sync = self.journal._sync_directory
        failed = False

        def sync(path):
            nonlocal failed
            path = Path(path)
            events.append(path)
            if path == self.root.parent and not failed:
                failed = True
                raise OSError("first parent barrier failed")
            real_sync(path)

        with mock.patch.object(self.journal, "_sync_directory", side_effect=sync):
            with self.assertRaisesRegex(OSError, "first parent barrier"):
                self.journal.begin(self.root, "preparation-only", 123)
            self.assertTrue(self.root.is_dir())
            self.assertFalse(any(self.root.glob("*/plan.json")))
            events.clear()
            transaction = self.journal.begin(self.root, "preparation-only-retry", 124)
        self.assertIn(self.root.parent, events)
        self.assertIn(self.root.parent.parent, events)
        self.assertEqual(self.journal.incomplete(self.root)[0]["id"], transaction.name)

    def test_failed_ancestor_barrier_is_repeated_before_next_atomic_record(self):
        target = self.root / "orphan/prepared.apply.pending.json"
        real_sync = self.journal._sync_directory
        events = []
        failed = False

        def sync(path):
            nonlocal failed
            path = Path(path)
            events.append(path)
            if path == self.root.parent and not failed:
                failed = True
                raise OSError("first ancestor barrier failed")
            real_sync(path)

        with mock.patch.object(self.journal, "_sync_directory", side_effect=sync):
            with self.assertRaisesRegex(OSError, "first ancestor barrier"):
                self.journal.atomic_json(target, {"status": "UNKNOWN"})
            self.assertTrue(target.parent.is_dir())
            self.assertFalse(target.exists())
            events.clear()
            self.journal.atomic_json(target, {"status": "UNKNOWN"})
        self.assertIn(self.root.parent, events)
        self.assertIn(self.root.parent.parent, events)
        self.assertEqual(json.loads(target.read_text())["status"], "UNKNOWN")

    @unittest.skipUnless(sys.platform == "linux", "native Linux directory fsync required")
    def test_new_directory_entries_and_atomic_record_are_synced_before_return(self):
        real_sync, real_replace = os.fsync, os.replace
        events = []

        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                events.append(("directory", Path(os.readlink(f"/proc/self/fd/{fd}"))))
            real_sync(fd)

        def replace(source, target):
            real_replace(source, target)
            events.append(("rename", Path(target)))

        with mock.patch.object(self.journal.os, "fsync", side_effect=sync), \
             mock.patch.object(self.journal.os, "replace", side_effect=replace):
            transaction = self.pending()
        first_record = events.index(("rename", transaction / "plan.json"))
        self.assertIn(("directory", self.root.parent.parent), events[:first_record])
        self.assertIn(("directory", self.root.parent), events[:first_record])
        self.assertIn(("directory", self.root), events[:first_record])
        pending_rename = events.index(("rename", transaction / "prepared.apply.pending.json"))
        self.assertIn(("directory", transaction), events[pending_rename + 1:])
        self.assertEqual(stat.S_IMODE(transaction.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((transaction / "prepared.apply.pending.json").stat().st_mode), 0o600)

    @unittest.skipUnless(sys.platform == "linux", "native Linux directory fsync required")
    def test_pending_directory_sync_failure_stops_before_dispatch_and_keeps_debt(self):
        transaction = self.journal.begin(self.root, "auto-prepared-reconcile", 123)
        real_sync = os.fsync
        dispatched = []

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode) and (transaction / "prepared.apply.pending.json").exists():
                raise OSError("synthetic directory sync failure")
            real_sync(fd)

        with mock.patch.object(self.journal.os, "fsync", side_effect=fail_directory):
            with self.assertRaisesRegex(OSError, "directory sync failure"):
                self.journal.atomic_json(transaction / "prepared.apply.pending.json", {"status": "UNKNOWN"})
                dispatched.append("native mutation")
        self.assertEqual(dispatched, [])
        self.assert_retained(transaction)

    @unittest.skipUnless(sys.platform == "linux", "native Linux directory fsync required")
    def test_new_transaction_directory_sync_failure_prevents_record_or_dispatch(self):
        # Existing root isolates the barrier that records the new tx entry.
        self.root.mkdir(parents=True)
        real_sync = self.journal._sync_directory

        def fail_new_entry(path):
            if Path(path) == self.root and any(self.root.iterdir()):
                raise OSError("parent sync failure")
            real_sync(path)

        with mock.patch.object(self.journal, "_sync_directory", side_effect=fail_new_entry), \
             mock.patch.object(self.journal, "atomic_json") as write:
            with self.assertRaisesRegex(OSError, "parent sync failure"):
                self.journal.begin(self.root, "must-not-dispatch", 123)
        write.assert_not_called()
        self.assertFalse(any(self.root.glob("*/plan.json")))


if __name__ == "__main__":
    unittest.main()
