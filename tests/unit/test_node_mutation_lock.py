"""CLI/Agent interprocess exclusion; every lock and state lives in a temp dir."""
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
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("mutation_runtime", ROOT / "agent/access_runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request = self.root / "request.json"
        self.config = self.root / "config.json"
        self.output = self.root / "result.json"
        self.transactions = self.root / "transactions"
        self.config.write_text('{}', encoding="utf-8")

    def transaction(self, status):
        directory = self.transactions / "20260101T000000Z-aaaaaa"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "plan.json").write_text('{"schema_version":1}', encoding="utf-8")
        if status is not None:
            (directory / "result.json").write_text(json.dumps({"status": status}), encoding="utf-8")
        return directory

    def invoke(self, operation="access.provision", cleanup=False):
        self.request.write_text(json.dumps({"operation": operation}), encoding="utf-8")
        argv = ["runtime", "--request", str(self.request), "--config", str(self.config),
                "--state-root", str(self.root / "access"), "--output", str(self.output)]
        if cleanup:
            argv.append("--cleanup-previous")
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as log:
            code = runtime.main()
        return code, log.getvalue()

    def test_all_operations_keep_node_lock_through_configuration_and_output(self):
        operations = [(name, "provision", False) for name in (
            "access.provision", "access.replace_credential", "access.prepare_replacement")]
        operations += [("access.update_entitlements", "update_entitlements", False),
                       ("access.replace_credential", "cleanup_previous", True)]
        for operation, function, cleanup in operations:
            with self.subTest(operation=operation, cleanup=cleanup):
                held = False

                @contextmanager
                def lock():
                    nonlocal held
                    self.assertFalse(held)
                    held = True
                    # Config changes immediately before acquisition must be read
                    # inside the critical section, not taken from a stale snapshot.
                    self.config.write_text('{"generation":2}', encoding="utf-8")
                    try:
                        yield
                    finally:
                        held = False

                def execute(command, config, state):
                    self.assertTrue(held)
                    self.assertEqual(config, {"generation": 2})
                    return 0 if cleanup else {"accepted": True}

                def persist(path, value):
                    self.assertTrue(held)
                    self.assertEqual(path, self.output)

                with mock.patch.object(runtime, "node_mutation_lock", lock), \
                     mock.patch.object(runtime, function, side_effect=execute) as executor, \
                     mock.patch.object(runtime, "atomic_json", side_effect=persist) as output:
                    code, _ = self.invoke(operation, cleanup)
                self.assertEqual(code, 0)
                executor.assert_called_once()
                self.assertEqual(output.call_count, 0 if cleanup else 1)
                self.assertFalse(held)

    def test_pending_and_uncertain_transaction_blocks_every_executor(self):
        for status in (None, "in_progress", "recovering", "rollback_failed", "future_status"):
            directory = self.transaction(status)
            for cleanup in (False, True):
                with self.subTest(status=status, cleanup=cleanup), \
                     mock.patch.object(runtime, "node_mutation_lock"), \
                     mock.patch.object(runtime, "provision") as provision, \
                     mock.patch.object(runtime, "cleanup_previous") as cleanup_fn, \
                     mock.patch.object(runtime, "update_entitlements") as update:
                    code, log = self.invoke("access.update_entitlements", cleanup)
                    self.assertEqual(code, 1)
                    self.assertEqual(log, "access_runtime=FAIL code=PROVISIONERROR\n")
                    provision.assert_not_called()
                    cleanup_fn.assert_not_called()
                    update.assert_not_called()
                    self.assertFalse(self.output.exists())
            shutil.rmtree(directory)

    def test_only_known_terminal_transactions_allow_execution(self):
        for status in ("committed", "rolled_back"):
            self.transaction(status)
            with mock.patch.object(runtime, "node_mutation_lock"), \
                 mock.patch.object(runtime, "provision", return_value={}) as executor:
                self.assertEqual(self.invoke()[0], 0)
                executor.assert_called_once()

    def test_malformed_or_missing_transaction_evidence_blocks_without_raw_error(self):
        directory = self.transaction("committed")
        for name, text in (("result.json", "secret-invalid-json"),
                           ("result.json", '[]'), ("plan.json", '{"schema_version":99}'),
                           ("plan.json", '{"schema_version":true}')):
            self.transaction("committed")
            (directory / name).write_text(text, encoding="utf-8")
            with mock.patch.object(runtime, "node_mutation_lock"), \
                 mock.patch.object(runtime, "provision") as executor:
                code, log = self.invoke()
                self.assertEqual(code, 1)
                self.assertNotIn("secret", log)
                executor.assert_not_called()
        self.transaction("committed")
        (directory / "plan.json").unlink()
        with self.assertRaises(runtime.ProvisionError):
            runtime.assert_no_pending_node_transaction(self.transactions)


@unittest.skipUnless(os.name == "posix" and shutil.which("bash") and shutil.which("flock"),
                     "Requires Linux flock and Bash; exercised in Linux CI")
class ProcessLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = self.root / "node.lock"
        # Execute the real CLI function, redirecting only its fixed runtime paths
        # to isolated fixture paths. flock is the system binary, not a mock.
        source = (ROOT / "scripts/lib/transaction.sh").read_text(encoding="utf-8")
        self.assertIn("/run/lock/wavemesh-node.lock", source)
        source = source.replace("/run/lock/wavemesh-node.lock", str(self.lock))
        source = source.replace("mkdir -p /run/lock", ":")
        self.cli = self.root / "transaction.sh"
        self.cli.write_text(source, encoding="utf-8")

    def cli_command(self, hold=False):
        return ["bash", "-c", 'set -e; WM_STATE_DIR="$2"; wm_fail() { return 23; }; '
                'source "$1"; wm_lock_mutation fixture; printf "LOCKED\\n"; '
                + ('read -r done' if hold else ':'), "fixture", str(self.cli), str(self.root)]

    def holder(self, command):
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        timer = threading.Timer(10, process.kill)
        timer.start()
        try:
            self.assertEqual(process.stdout.readline().strip(), "LOCKED")
        except BaseException:
            process.kill()
            process.communicate(timeout=5)
            raise
        finally:
            timer.cancel()
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def test_cli_blocks_agent_and_process_death_releases_same_inode(self):
        child = self.holder(self.cli_command(hold=True))
        inode = self.lock.stat().st_ino
        with self.assertRaisesRegex(runtime.ProvisionError, "busy"):
            with runtime.node_mutation_lock(self.lock):
                self.fail("overlapping Agent mutation")
        self.stop(child)
        with runtime.node_mutation_lock(self.lock):
            self.assertEqual(self.lock.stat().st_ino, inode)

    def test_agent_blocks_real_cli_and_second_agent(self):
        with runtime.node_mutation_lock(self.lock):
            child = subprocess.run(self.cli_command(), capture_output=True, timeout=5)
            self.assertNotEqual(child.returncode, 0)
            self.assertNotIn(b"LOCKED", child.stdout)
            with self.assertRaisesRegex(runtime.ProvisionError, "busy"):
                with runtime.node_mutation_lock(self.lock):
                    self.fail("overlapping second Agent")
        child = subprocess.run(self.cli_command(), capture_output=True, timeout=5)
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_agent_death_releases_lock_without_deleting_it(self):
        code = """import importlib.util,pathlib,sys
spec=importlib.util.spec_from_file_location('fixture_runtime',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with m.node_mutation_lock(pathlib.Path(sys.argv[2])):
 print('LOCKED',flush=True)
 sys.stdin.read()
"""
        child = self.holder([sys.executable, "-c", code, str(ROOT / "agent/access_runtime.py"), str(self.lock)])
        inode = self.lock.stat().st_ino
        self.stop(child)
        result = subprocess.run(self.cli_command(), capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lock.stat().st_ino, inode)

    def test_existing_lock_content_and_inode_are_preserved(self):
        self.lock.write_text("sentinel", encoding="utf-8")
        inode = self.lock.stat().st_ino
        with runtime.node_mutation_lock(self.lock):
            self.assertEqual(self.lock.read_text(), "sentinel")
        self.assertEqual(self.lock.stat().st_ino, inode)

    def test_unsafe_lock_files_are_rejected(self):
        target = self.root / "target"
        target.write_text("sentinel", encoding="utf-8")
        for kind in ("symlink", "hardlink", "fifo", "directory", "writable"):
            with self.subTest(kind=kind):
                if kind == "symlink": self.lock.symlink_to(target)
                elif kind == "hardlink": os.link(target, self.lock)
                elif kind == "fifo": os.mkfifo(self.lock)
                elif kind == "directory": self.lock.mkdir()
                else:
                    self.lock.touch()
                    self.lock.chmod(0o666)
                with self.assertRaises((OSError, runtime.ProvisionError)):
                    with runtime.node_mutation_lock(self.lock):
                        self.fail("unsafe lock accepted")
                self.assertEqual(target.read_text(), "sentinel")
                if kind == "directory": self.lock.rmdir()
                else: self.lock.unlink()

    def test_tmpfiles_creation_preserves_locked_inode_and_content(self):
        if not shutil.which("systemd-tmpfiles"):
            self.skipTest("systemd-tmpfiles is unavailable")
        rule = self.root / "fixture.conf"
        source = (ROOT / "agent/wavemesh-node-lock.conf").read_text()
        # Root ownership in the actual rule is an installation prerequisite.
        # Fixture uses current UID/GID and never writes a host runtime path.
        source = source.replace("/run/lock/wavemesh-node.lock", str(self.lock))
        source = source.replace("root root", f"{os.getuid()} {os.getgid()}")
        rule.write_text(source)
        result = subprocess.run(["systemd-tmpfiles", "--create", str(rule)], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.lock.write_text("sentinel")
        inode = self.lock.stat().st_ino
        with runtime.node_mutation_lock(self.lock):
            result = subprocess.run(["systemd-tmpfiles", "--create", str(rule)], capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.lock.stat().st_ino, inode)
            self.assertEqual(self.lock.read_text(), "sentinel")
            child = subprocess.run(self.cli_command(), capture_output=True, timeout=5)
            self.assertNotEqual(child.returncode, 0)


if __name__ == "__main__":
    unittest.main()
