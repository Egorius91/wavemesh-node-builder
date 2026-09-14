"""Synthetic local transport/process tests; never contact a real panel."""
import importlib.util
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
import panel_request_guard as guard
import access_runtime as runtime


class ContractTests(unittest.TestCase):
    def test_allowlist_and_read_only_post_contract(self):
        for path in ("/panel/api/clients/add", "/panel/api/clients/addDisabled", "/panel/api/clients/update/synthetic_client",
                     "/panel/api/inbounds/update/1", "/panel/api/xray/update",
                     "/panel/api/setting/apiTokens/create"):
            self.assertTrue(guard.mutation("POST", path))
        for path in guard.READ_POSTS:
            self.assertFalse(guard.mutation("POST", path))
        for method, path in (("DELETE", "/panel/api/clients/add"), ("POST", "/arbitrary"),
                             ("POST", "/panel/api/inbounds/del/../1")):
            with self.assertRaises(guard.PanelRequestError):
                guard.mutation(method, path)

    def test_duplicate_or_malformed_success_is_not_acceptance(self):
        for raw in (b'{}', b'[]', b'{', b'{"success":1}', b'{"success":false}',
                    b'{"success":false,"success":true}'):
            self.assertFalse(guard.response_accepted(raw))
        self.assertTrue(guard.response_accepted(b'{"success":true}'))


@unittest.skipUnless(os.name == "posix", "POSIX node filesystem/process contract")
class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "journal"
        self.journal = guard.PanelRequestGuard(self.state)
        self.environment = mock.patch.dict(os.environ, {"WAVEMESH_PANEL_REQUEST_STATE_DIR": str(self.state)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def execute(self, dispatch=None):
        return self.journal.execute("POST", "/panel/api/clients/add", "private_target_canary",
                                    {"uuid": "private_identity_canary"},
                                    dispatch or (lambda: ("200", b'{"success":true}')))

    def record(self):
        return json.loads((self.state / "state.json").read_text())

    def test_success_allows_next_request_without_claiming_runtime(self):
        self.execute()
        first = self.record()
        self.assertEqual(first["phase"], "RESPONSE_ACCEPTED")
        self.execute()
        second = self.record()
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertNotEqual(first["request_digest"], second["request_digest"])
        self.assertNotIn("private_", json.dumps(second))
        self.assertEqual((self.state / "state.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)

    def test_timeout_retains_intent_and_blocks_next_write_but_not_reads(self):
        dispatch = mock.Mock(side_effect=TimeoutError("secret_error_canary"))
        with self.assertRaisesRegex(guard.PanelRequestError, "^PANEL_REQUEST_UNCERTAIN$"):
            self.execute(dispatch)
        self.assertEqual(self.record()["phase"], "DISPATCH_INTENT")
        with self.assertRaisesRegex(guard.PanelRequestError, "RECONCILIATION_REQUIRED"):
            self.execute(dispatch)
        self.assertEqual(dispatch.call_count, 1)
        read = mock.Mock(return_value=("200", b'{"success":true}'))
        self.journal.execute("GET", "/panel/api/clients/list", "target", None, read)
        read.assert_called_once()
        self.assertEqual(self.record()["phase"], "DISPATCH_INTENT")

    def test_rejected_and_malformed_response_leave_uncertainty(self):
        for raw in (b'{"success":false}', b'invalid', b'{"success":false,"success":true}'):
            journal = guard.PanelRequestGuard(self.root / str(len(raw)))
            with self.assertRaisesRegex(guard.PanelRequestError, "RESPONSE_UNCERTAIN"):
                journal.execute("POST", "/panel/api/clients/add", "target", {}, lambda: ("200", raw))
            with journal.locked():
                self.assertEqual(journal.load()["phase"], "DISPATCH_INTENT")

    def test_fsync_failure_before_dispatch_performs_no_network(self):
        dispatch = mock.Mock()
        with mock.patch.object(guard.os, "fsync", side_effect=OSError("private_disk_error")):
            with self.assertRaises(guard.PanelRequestError):
                self.execute(dispatch)
        dispatch.assert_not_called()

    def test_completion_directory_fsync_failure_restores_pending(self):
        real = os.fsync
        dispatched = False
        failed = False
        def dispatch():
            nonlocal dispatched
            dispatched = True
            return ("200", b'{"success":true}')
        def fail_completion(fd):
            nonlocal failed
            if dispatched and not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
                failed = True
                raise OSError("private_disk_error")
            return real(fd)
        with mock.patch.object(guard.os, "fsync", side_effect=fail_completion):
            with self.assertRaises(guard.PanelRequestError):
                self.execute(dispatch)
        self.assertTrue(failed)
        self.assertEqual(self.record()["phase"], "DISPATCH_INTENT")

    def test_new_directory_ancestry_is_synced_before_dispatch(self):
        self.journal = guard.PanelRequestGuard(self.root / "new-parent" / "journal")
        synced = set()
        real = os.fsync
        def sync(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                synced.add((info.st_dev, info.st_ino))
            return real(fd)
        def dispatch():
            for path in (self.journal.root, *self.journal.root.parents):
                info = path.stat()
                self.assertIn((info.st_dev, info.st_ino), synced)
            return ("200", b'{"success":true}')
        with mock.patch.object(guard.os, "fsync", side_effect=sync):
            self.execute(dispatch)

    def test_unsafe_and_unknown_state_blocks_before_network(self):
        self.execute()
        state = self.state / "state.json"
        for raw in ('{}', '{"schema_version":1,"schema_version":1}', '[1]'):
            state.write_text(raw)
            dispatch = mock.Mock()
            with self.assertRaises(guard.PanelRequestError):
                self.execute(dispatch)
            dispatch.assert_not_called()
        state.unlink()
        outside = self.root / "outside"
        outside.write_text('{}')
        outside.chmod(0o600)
        state.symlink_to(outside)
        with self.assertRaises(guard.PanelRequestError):
            self.execute()
        state.unlink()
        os.link(outside, state)
        with self.assertRaises(guard.PanelRequestError):
            self.execute()
        state.unlink()
        os.mkfifo(state, 0o600)
        with self.assertRaises(guard.PanelRequestError):
            self.execute()

    def test_process_crash_retains_barrier_after_flock_release(self):
        script = 'import os; from panel_request_guard import PanelRequestGuard; PanelRequestGuard().execute("POST", "/panel/api/clients/add", "target", {}, lambda: os._exit(17))'
        env = {**os.environ, "PYTHONPATH": str(ROOT / "agent")}
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(self.record()["phase"], "DISPATCH_INTENT")
        with self.assertRaisesRegex(guard.PanelRequestError, "RECONCILIATION_REQUIRED"):
            self.execute()

    def test_concurrent_process_cannot_dispatch_while_first_is_running(self):
        marker = self.root / "entered"
        release = self.root / "release"
        script = '''import time
from pathlib import Path
from panel_request_guard import PanelRequestGuard
def dispatch():
 Path(__MARKER__).touch()
 while not Path(__RELEASE__).exists(): time.sleep(.01)
 return ('200', b'{"success":true}')
PanelRequestGuard().execute('POST', '/panel/api/clients/add', 'target', {}, dispatch)
'''.replace('__MARKER__', repr(str(marker))).replace('__RELEASE__', repr(str(release)))
        child = subprocess.Popen([sys.executable, "-c", script], env={**os.environ, "PYTHONPATH": str(ROOT / "agent")})
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            dispatch = mock.Mock()
            with self.assertRaisesRegex(guard.PanelRequestError, "BUSY"):
                self.execute(dispatch)
            dispatch.assert_not_called()
        finally:
            release.touch()
            child.wait(timeout=5)
        self.assertEqual(child.returncode, 0)

    def shell(self, mode="success", method="POST"):
        binary = self.root / "bin"
        binary.mkdir(exist_ok=True)
        curl = binary / "curl"
        curl.write_text('#!' + sys.executable + '''
import os,sys
from pathlib import Path
args=sys.argv[1:]
counter=Path(os.environ['PANEL_TEST_COUNTER'])
counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')
Path(args[args.index('--output')+1]).write_text('{"success":true}')
if os.environ['PANEL_TEST_MODE']=='timeout': sys.exit(28)
print('200', end='')
''')
        curl.chmod(0o700)
        script = 'source "$PANEL_TEST_SOURCE"; wm_warn() { echo "$*" >&2; }; PANEL_PORT=12345; PANEL_PATH=/synthetic/; PANEL_TOKEN=synthetic_token; wm_xui_request_success "$PANEL_TEST_METHOD" /panel/api/clients/add json "{}"'
        env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
               "PANEL_TEST_SOURCE": str(ROOT / "scripts/lib/xui_api.sh"), "PANEL_TEST_MODE": mode,
               "PANEL_TEST_METHOD": method, "PANEL_TEST_COUNTER": str(self.root / "counter")}
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)

    def test_shell_timeout_blocks_python_and_shell_but_allows_read(self):
        failed = self.shell("timeout")
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn("synthetic_token", failed.stderr)
        panel = runtime.PanelClient({"panel": {"listen_port": 12345, "path": "synthetic", "api_auth": {"token": "synthetic_token"}}})
        with mock.patch.object(runtime.request, "urlopen") as network:
            with self.assertRaisesRegex(runtime.ProvisionError, "RECONCILIATION_REQUIRED"):
                panel.call("POST", "/panel/api/clients/add", {})
            network.assert_not_called()
        self.assertNotEqual(self.shell().returncode, 0)
        self.assertEqual((self.root / "counter").read_text(), "1")
        self.assertEqual(self.shell(method="GET").returncode, 0)
        self.assertEqual((self.root / "counter").read_text(), "2")

    def test_python_transport_timeout_blocks_shell_before_curl(self):
        panel = runtime.PanelClient({"panel": {"listen_port": 12345, "path": "synthetic", "api_auth": {"token": "synthetic_token"}}})
        def timeout(*args, **kwargs):
            self.assertEqual(self.record()["phase"], "DISPATCH_INTENT")
            raise TimeoutError("private_network_error")
        with mock.patch.object(runtime.request, "urlopen", side_effect=timeout) as network:
            with self.assertRaisesRegex(runtime.ProvisionError, "^PANEL_REQUEST_UNCERTAIN$"):
                panel.call("POST", "/panel/api/clients/add", {})
            network.assert_called_once()
        self.assertNotEqual(self.shell().returncode, 0)
        self.assertFalse((self.root / "counter").exists())

    def test_python_success_preserves_response_and_allows_shell(self):
        panel = runtime.PanelClient({"panel": {"listen_port": 12345, "path": "synthetic", "api_auth": {"token": "synthetic_token"}}})
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"success":true,"obj":{"count":1}}'
        with mock.patch.object(runtime.request, "urlopen", return_value=response):
            self.assertEqual(panel.call("POST", "/panel/api/clients/add", {}),
                             {"success": True, "obj": {"count": 1}})
        self.assertEqual(self.record()["phase"], "RESPONSE_ACCEPTED")
        self.assertEqual(self.shell().returncode, 0)
        self.assertEqual((self.root / "counter").read_text(), "1")

    def test_normal_shell_writes_continue_with_response_acceptance(self):
        self.assertEqual(self.shell().returncode, 0)
        self.assertEqual(self.shell().returncode, 0)
        self.assertEqual(self.record()["phase"], "RESPONSE_ACCEPTED")
        self.assertEqual((self.root / "counter").read_text(), "2")

    def test_uncertainty_blocks_cli_transaction_begin_and_snapshot_rollback(self):
        with self.assertRaises(guard.PanelRequestError):
            self.execute(mock.Mock(side_effect=TimeoutError()))
        marker = self.root / "mutation"
        script = '''source "$PANEL_TEST_TRANSACTION"
wm_warn() { :; }; wm_fail() { return 1; }
wm_atomic_install_json() { touch "$PANEL_TEST_MARKER"; }
wm_load_config() { touch "$PANEL_TEST_MARKER"; }
systemctl() { touch "$PANEL_TEST_MARKER"; }
if wm_transaction_begin synthetic; then exit 21; fi
if wm_transaction_rollback "$WM_STATE_DIR" synthetic; then exit 22; fi
'''
        result = subprocess.run(["bash", "-c", script], capture_output=True, env={
            **os.environ, "WM_STATE_DIR": str(self.root),
            "PANEL_TEST_TRANSACTION": str(ROOT / "scripts/lib/transaction.sh"),
            "PANEL_TEST_MARKER": str(marker)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "transactions").exists())
        self.assertEqual(self.record()["phase"], "DISPATCH_INTENT")


if __name__ == "__main__":
    unittest.main()
