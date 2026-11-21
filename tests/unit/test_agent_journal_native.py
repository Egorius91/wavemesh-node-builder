"""Actual POSIX journal barrier and shared-lock admission, synthetic files only."""
import importlib.util
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_prepared_journal import completed_fixture
SPEC = importlib.util.spec_from_file_location("native_journal_runtime", ROOT / "agent/access_runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


@unittest.skipUnless(sys.platform.startswith("linux"), "Requires Linux directory fsync and flock")
class NativeAgentJournalAdmission(unittest.TestCase):
    def test_actual_directory_barrier_and_fault_precede_panel_effect(self):
        import fcntl
        with tempfile.TemporaryDirectory(prefix="wm-agent-journal-native-") as temporary:
            root = Path(temporary)
            directory = completed_fixture(root / "transactions")
            config, request = root / "config.json", root / "request.json"
            config.write_text('{}')
            request.write_text('{"operation":"access.provision"}')
            args = ["runtime", "--request", str(request), "--config", str(config),
                    "--state-root", str(root / "access"), "--output", str(root / "output.json")]
            lock = root / "node.lock"
            acquire, fsync = runtime.node_mutation_lock, os.fsync
            inode = directory.stat().st_ino
            for fail in (True, False):
                with self.subTest(fail=fail):
                    events = []

                    def barrier(descriptor):
                        info = os.fstat(descriptor)
                        if stat.S_ISDIR(info.st_mode) and info.st_ino == inode:
                            with lock.open('rb') as competitor:
                                with self.assertRaises(BlockingIOError):
                                    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            events.append("barrier")
                            if fail:
                                raise OSError("synthetic directory barrier failed")
                        fsync(descriptor)

                    def execute(*_):
                        self.assertEqual(events, ["barrier"])
                        events.append("effect")
                        return {}

                    with mock.patch.object(sys, "argv", args), \
                            mock.patch.object(runtime, "node_mutation_lock", lambda: acquire(lock)), \
                            mock.patch.object(os, "fsync", side_effect=barrier), \
                            mock.patch.object(runtime, "provision", side_effect=execute) as provision, \
                            redirect_stdout(io.StringIO()) as output:
                        code = runtime.main()
                    self.assertEqual(code, 1 if fail else 0, output.getvalue())
                    self.assertEqual(events, ["barrier"] if fail else ["barrier", "effect"])
                    self.assertEqual(provision.call_count, 0 if fail else 1)
                    with acquire(lock):
                        pass
            # Even a dangling pending origin dominates a terminal result.
            (directory / "prepared.apply.pending.json").symlink_to(root / "absent")
            with self.assertRaises(runtime.ProvisionError):
                runtime.assert_no_pending_node_transaction(root / "transactions")


if __name__ == "__main__":
    unittest.main()
