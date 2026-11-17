"""Agent admission consumes the Builder reader without changing read-only polls."""
from contextlib import contextmanager, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_prepared_journal import completed_fixture

SPEC = importlib.util.spec_from_file_location("prepared_access_runtime", ROOT / "agent/access_runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class AgentPreparedJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wm-agent-journal-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transactions = self.root / "transactions"

    def test_pending_dominates_terminal_result_and_stays_read_only(self):
        directory = completed_fixture(self.transactions)
        (directory / "prepared.apply.pending.json").write_text("broken", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        with mock.patch.object(os, "fsync", side_effect=AssertionError("read-only poll wrote")), \
                self.assertRaises(runtime.ProvisionError):
            runtime.assert_no_pending_node_transaction(self.transactions)
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_only_verified_completion_returns_barrier_work_without_performing_it(self):
        directory = completed_fixture(self.transactions)
        with mock.patch.object(os, "fsync", side_effect=AssertionError("read-only poll wrote")):
            self.assertEqual(runtime.assert_no_pending_node_transaction(self.transactions), [directory])
        result = json.loads((directory / "result.json").read_text())
        result["prepared_commit"]["selector_transition"]["to_epoch"] = "2"
        (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaises(runtime.ProvisionError):
            runtime.assert_no_pending_node_transaction(self.transactions)

    def invoke(self):
        request, config, output = (self.root / name for name in ("request.json", "config.json", "output.json"))
        request.write_text('{"operation":"access.provision"}', encoding="utf-8")
        config.write_text('{}', encoding="utf-8")
        args = ["runtime", "--request", str(request), "--config", str(config), "--output", str(output),
                "--state-root", str(self.root / "access")]
        with mock.patch.object(sys, "argv", args), redirect_stdout(io.StringIO()) as log:
            result = runtime.main()
        return result, log.getvalue(), output

    def test_writer_barrier_precedes_effects_and_failure_refuses_execution(self):
        directory = completed_fixture(self.transactions)
        held = False

        @contextmanager
        def lock():
            nonlocal held
            held = True
            try:
                yield
            finally:
                held = False

        events = []

        def barrier(path):
            self.assertTrue(held)
            self.assertEqual(path, directory)
            events.append("barrier")

        def execute(*_):
            self.assertTrue(held)
            self.assertEqual(events, ["barrier"])
            events.append("effect")
            return {}

        with mock.patch.object(runtime, "node_mutation_lock", lock), \
                mock.patch.object(runtime, "sync_directory", side_effect=barrier) as sync, \
                mock.patch.object(runtime, "provision", side_effect=execute) as provision, \
                mock.patch.object(runtime, "atomic_json"):
            self.assertEqual(self.invoke()[0], 0)
            sync.assert_called_once_with(directory)
            provision.assert_called_once()
        with mock.patch.object(runtime, "node_mutation_lock", lock), \
                mock.patch.object(runtime, "sync_directory", side_effect=OSError("private barrier detail")), \
                mock.patch.object(runtime, "provision") as provision:
            code, log, output = self.invoke()
            self.assertEqual(code, 1)
            self.assertEqual(log, "access_runtime=FAIL code=OSERROR\n")
            provision.assert_not_called()
            self.assertFalse(output.exists())

    def test_installed_only_reader_is_identical_and_required(self):
        installed = self.root / "installed"
        installed.mkdir()
        for source in (ROOT / "agent/access_runtime.py", ROOT / "scripts/lib/prepared_journal.py"):
            shutil.copyfile(source, installed / source.name)
        self.assertEqual((installed / "prepared_journal.py").read_bytes(),
                         (ROOT / "scripts/lib/prepared_journal.py").read_bytes())
        program = """import pathlib,sys
sys.path.insert(0,sys.argv[1])
import access_runtime,prepared_journal
assert pathlib.Path(prepared_journal.__file__).parent==pathlib.Path(sys.argv[1])
assert access_runtime.assert_no_pending_node_transaction(pathlib.Path(sys.argv[2]))==[]
"""
        args = [sys.executable, "-I", "-B", "-c", program, str(installed), str(self.root / "absent")]
        accepted = subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        (installed / "prepared_journal.py").unlink()
        refused = subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("prepared_journal", refused.stderr)


if __name__ == "__main__":
    unittest.main()
