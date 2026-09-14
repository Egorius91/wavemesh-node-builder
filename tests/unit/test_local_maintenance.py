#!/usr/bin/env python3
"""Durable admission, process races and interrupted storage on real POSIX files."""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
import panel_request_guard as module
import access_runtime as runtime

OP = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"


class IdentityTest(unittest.TestCase):
    def test_rejects_noncanonical_or_untyped_identity(self):
        for operation in (None, "", "not-a-uuid", "{" + OP + "}", OP.replace("-", "")):
            with self.subTest(operation=operation), self.assertRaises(module.PanelRequestError):
                module.validate_hold_identity(operation, 1)
        for generation in (True, False, None, "1", 1.0, 0, -1, 2147483648):
            with self.subTest(generation=generation), self.assertRaises(module.PanelRequestError):
                module.validate_hold_identity(OP, generation)


@unittest.skipUnless(os.name == "posix", "real flock/fsync tests run on Linux CI")
class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = self.root / "node.lock"
        self.guard = module.PanelRequestGuard(self.root / "journal")
        self.env = dict(os.environ, WAVEMESH_PANEL_REQUEST_STATE_DIR=str(self.guard.root))

    def act(self, action="prepare", operation=OP, generation=1):
        with module.maintenance_node_lock(self.lock), self.guard.locked():
            return self.guard.maintenance(action, operation, generation)

    def read(self):
        with self.guard.locked():
            return self.guard.load()

    def write(self):
        return self.guard.execute("POST", "/panel/api/clients/add", "fixture", {},
                                  lambda: (200, b'{"success":true}'))

    def child(self, code, *args):
        return subprocess.run([sys.executable, "-c", code, str(ROOT / "agent"),
                               str(self.lock), *args], env=self.env,
                              capture_output=True, timeout=10)

    def holder(self, code):
        child = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "agent"), str(self.lock)],
                                 env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, child)
        timer = threading.Timer(10, child.kill)
        timer.start()
        try:
            self.assertEqual(child.stdout.readline().strip(), "READY")
        finally:
            timer.cancel()
        return child

    @staticmethod
    def stop(child):
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)

    def test_cancelled_replay_never_recloses_and_generations_fence_old_commands(self):
        self.assertEqual(self.act()["local_admission"], "CLOSED")
        self.assertEqual(self.act()["quiescence"], "NOT_PROVEN")
        self.act("cancel")
        before = self.read()
        self.assertEqual(self.act()["local_admission"], "NOT_HELD")
        self.assertEqual(self.read(), before)
        for action, operation, generation in (("cancel", OTHER, 1), ("prepare", OTHER, 3),
                                               ("prepare", OTHER, 1)):
            with self.assertRaises(module.PanelRequestError):
                self.act(action, operation, generation)
        self.act("prepare", OTHER, 2)
        for action in ("prepare", "cancel"):
            with self.assertRaises(module.PanelRequestError):
                self.act(action)
        self.assertEqual(self.read()["maintenance"]["generation"], 2)

    def test_v1_accepted_history_migrates_and_survives_later_requests(self):
        self.write()
        v1 = self.read()
        self.assertEqual(v1["schema_version"], 1)
        self.act()
        self.assertEqual(self.read()["request"], v1)
        self.act("cancel")
        self.write()
        state = self.read()
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["maintenance"]["phase"], "CANCELLED")
        self.assertNotEqual(state["request"]["attempt_id"], v1["attempt_id"])
        self.assertEqual(self.act()["local_admission"], "NOT_HELD")

    def test_uncertain_request_is_preserved_and_cannot_be_cancelled_away(self):
        def lost():
            raise TimeoutError("synthetic-private-error")
        with self.assertRaises(module.PanelRequestError):
            self.guard.execute("POST", "/panel/api/clients/add", "fixture", {}, lost)
        pending = self.read()
        self.assertTrue(self.act()["request_pending"])
        self.assertEqual(self.read()["request"], pending)
        with self.assertRaisesRegex(module.PanelRequestError, "RECONCILIATION_REQUIRED"):
            self.act("cancel")
        self.assertEqual(self.read()["maintenance"]["phase"], "HELD")

    def test_old_cancel_replay_does_not_clear_a_later_uncertain_request(self):
        self.act()
        self.act("cancel")
        with self.assertRaises(module.PanelRequestError):
            self.guard.execute("POST", "/panel/api/clients/add", "fixture", {}, lambda: (200, b"invalid"))
        before = self.read()
        self.assertTrue(self.act("cancel")["request_pending"])
        self.assertEqual(self.read(), before)
        with self.assertRaises(module.PanelRequestError):
            self.write()

    def test_process_death_keeps_hold_and_lost_prepare_response_replays(self):
        child = self.holder('''import pathlib,sys
sys.path.insert(0,sys.argv[1])
from panel_request_guard import PanelRequestGuard,maintenance_node_lock
g=PanelRequestGuard()
with maintenance_node_lock(pathlib.Path(sys.argv[2])),g.locked():
 g.maintenance('prepare', '00000000-0000-4000-8000-000000000001', 1)
print('READY',flush=True)
sys.stdin.read()
''')
        self.stop(child)
        self.assertEqual(self.act()["local_admission"], "CLOSED")
        result = self.child('''import pathlib,sys
sys.path.insert(0,sys.argv[1])
from access_runtime import node_mutation_lock
with node_mutation_lock(pathlib.Path(sys.argv[2])):
 pathlib.Path(sys.argv[3]).write_text('unexpected')
''', str(self.root / "effect"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "effect").exists())
        with self.assertRaisesRegex(module.PanelRequestError, "MAINTENANCE_HELD"):
            self.write()
        self.assertEqual(self.guard.execute("GET", "/panel/api/inbounds/list", "fixture", {},
                                           lambda: "read-ok"), "read-ok")

    def test_existing_node_writer_prevents_prepare_until_it_exits(self):
        child = self.holder('''import pathlib,sys
sys.path.insert(0,sys.argv[1])
from access_runtime import node_mutation_lock
with node_mutation_lock(pathlib.Path(sys.argv[2])):
 print('READY',flush=True)
 sys.stdin.read()
''')
        with self.assertRaisesRegex(module.PanelRequestError, "NODE_BUSY"):
            self.act()
        self.assertFalse((self.guard.root / "state.json").exists())
        self.stop(child)
        self.assertEqual(self.act()["local_admission"], "CLOSED")

    def test_agent_entrypoint_rejects_before_configuration_or_access_effects(self):
        self.act()
        for cleanup in (False, True):
            arguments = ["access_runtime.py", "--request", str(self.root / "missing-request"),
                         "--config", str(self.root / "missing-config"),
                         "--output", str(self.root / "output")]
            if cleanup:
                arguments.append("--cleanup-previous")
            with patch.dict(os.environ, self.env), patch.object(runtime, "NODE_MUTATION_LOCK", self.lock), \
                 patch.object(sys, "argv", arguments), patch.object(runtime, "provision") as provision, \
                 patch.object(runtime, "cleanup_previous") as remove, \
                 patch.object(runtime, "update_entitlements") as update, \
                 redirect_stdout(io.StringIO()) as output:
                self.assertEqual(runtime.main(), 1)
                self.assertEqual(output.getvalue(), "access_runtime=FAIL code=PROVISIONERROR\n")
                provision.assert_not_called()
                remove.assert_not_called()
                update.assert_not_called()
            self.assertFalse((self.root / "output").exists())

    def test_inflight_transport_prevents_prepare_and_killed_writer_keeps_uncertainty(self):
        child = self.holder('''import sys
sys.path.insert(0,sys.argv[1])
from panel_request_guard import PanelRequestGuard
def dispatch():
 print('READY',flush=True)
 sys.stdin.read()
 return 200,b'{"success":true}'
PanelRequestGuard().execute('POST','/panel/api/clients/add','fixture',{},dispatch)
''')
        with self.assertRaisesRegex(module.PanelRequestError, "REQUEST_BUSY"):
            self.act()
        self.stop(child)
        self.assertTrue(self.act()["request_pending"])
        with self.assertRaises(module.PanelRequestError):
            self.act("cancel")

    def test_corruption_and_unsafe_state_never_open_admission(self):
        self.act()
        valid = self.read()
        path = self.guard.root / "state.json"
        cases = ["{}", "[]", "not-json", json.dumps(valid).replace('"schema_version": 2',
                  '"schema_version": 2, "schema_version": 1')]
        for key, replacement in (("phase", "future"), ("generation", True), ("operation_id", "bad")):
            invalid = copy.deepcopy(valid)
            invalid["maintenance"][key] = replacement
            cases.append(json.dumps(invalid))
        for raw in cases:
            path.write_text(raw)
            with self.assertRaises((module.PanelRequestError, ValueError)):
                self.guard.check_maintenance()
            with self.assertRaises(module.PanelRequestError):
                self.write()
        path.write_text(json.dumps(valid))
        path.chmod(0o644)
        with self.assertRaises(module.PanelRequestError):
            self.act("cancel")
        path.chmod(0o600)
        target = self.root / "target"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaises((module.PanelRequestError, OSError)):
            self.guard.check_maintenance()
        self.assertEqual(json.loads(target.read_text()), valid)

    def test_prepare_replace_failure_gives_no_receipt_and_never_dispatches(self):
        with patch.object(module.os, "replace", side_effect=OSError("synthetic-disk")):
            with self.assertRaises(OSError):
                self.act()
        self.assertIsNone(self.read())
        self.assertFalse(list(self.guard.root.glob(".state-*")))
        self.assertEqual(self.act()["local_admission"], "CLOSED")

    def test_post_replace_sync_failure_requires_reconciliation_for_prepare_and_cancel(self):
        real_sync = os.fsync
        real_replace = os.replace
        replaced = False
        def replace(*args):
            nonlocal replaced
            real_replace(*args)
            replaced = True
        def sync(fd):
            if replaced and stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("synthetic-post-replace-sync-failure")
            real_sync(fd)
        for action in ("prepare", "cancel"):
            replaced = False
            with patch.object(module.os, "replace", side_effect=replace), \
                 patch.object(module.os, "fsync", side_effect=sync):
                with self.assertRaises(OSError):
                    self.act(action)
            # No successful receipt was returned. New-process observation is
            # required; a cancellation may already be visible after replace.
            result = self.child('''import json,sys
sys.path.insert(0,sys.argv[1])
from panel_request_guard import PanelRequestGuard
g=PanelRequestGuard()
with g.locked(): print(json.dumps(g.maintenance('status')))
''')
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout)
            self.assertEqual(observed["local_admission"], "CLOSED" if action == "prepare" else "NOT_HELD")
            self.assertEqual(self.act(action), observed)

    def test_failed_file_sync_cannot_publish_a_hold(self):
        real_sync = os.fsync
        def sync(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("synthetic-file-sync-failure")
            real_sync(fd)
        with patch.object(module.os, "fsync", side_effect=sync):
            with self.assertRaises(OSError):
                self.act()
        self.assertIsNone(self.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
