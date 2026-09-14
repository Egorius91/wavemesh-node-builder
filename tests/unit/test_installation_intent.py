#!/usr/bin/env python3
"""Pre-effect installation boundary on real locks, storage and process death."""
import copy
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

OP = "00000000-0000-4000-8000-000000000001"
CANDIDATE = "a" * 64
BACKUP = "b" * 64


class BindingTest(unittest.TestCase):
    def test_binding_requires_only_exact_sha256_fields_and_known_phase(self):
        valid = {"phase": "INSTALL_INTENT", "candidate_sha256": CANDIDATE,
                 "rollback_manifest_sha256": BACKUP}
        module.PanelRequestGuard.validate_installation(valid)
        for key in valid:
            for value in (None, True, 1, [], {}, "", "A" * 64, "a" * 63):
                with self.subTest(key=key, value=value), self.assertRaises(module.PanelRequestError):
                    module.PanelRequestGuard.validate_installation({**valid, key: value})
        with self.assertRaises(module.PanelRequestError):
            module.PanelRequestGuard.validate_installation({**valid, "force": True})


@unittest.skipUnless(os.name == "posix", "real flock/fsync/process tests run on Linux CI")
class IntentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = self.root / "node.lock"
        self.guard = module.PanelRequestGuard(self.root / "journal")
        self.env = dict(os.environ, WAVEMESH_PANEL_REQUEST_STATE_DIR=str(self.guard.root))
        self.maintenance("prepare")

    def maintenance(self, action):
        with module.maintenance_node_lock(self.lock), self.guard.locked():
            return self.guard.maintenance(action, OP, 1)

    def intent(self, operation=OP, generation=1, candidate=CANDIDATE, backup=BACKUP):
        return self.guard.installation_intent(operation, generation, candidate, backup, self.lock)

    def read(self):
        with self.guard.locked():
            return self.guard.load()

    def test_intent_is_durable_before_effects_and_keeps_both_locks(self):
        with self.intent() as result:
            self.assertFalse(result["reconciliation_required"])
            self.assertEqual(result["quiescence"], "NOT_PROVEN")
            state = json.loads((self.guard.root / "state.json").read_text())
            self.assertEqual(state["schema_version"], 3)
            self.assertEqual(state["installation"]["candidate_sha256"], CANDIDATE)
            with self.assertRaises(module.PanelRequestError):
                with module.maintenance_node_lock(self.lock):
                    self.fail("Node lock was released before effects")
            with self.assertRaises(module.PanelRequestError):
                with module.PanelRequestGuard(self.guard.root).locked():
                    self.fail("Journal lock was released before effects")
        for action in ("prepare", "cancel"):
            with self.assertRaisesRegex(module.PanelRequestError, "INSTALLATION_RECONCILIATION_REQUIRED"):
                self.maintenance(action)
        with self.assertRaises(module.PanelRequestError):
            self.guard.execute("POST", "/panel/api/clients/addDisabled", "fixture", {},
                               lambda: self.fail("write dispatched"))
        self.assertEqual(self.maintenance("status")["local_admission"], "CLOSED")

    def test_exception_and_replay_keep_exact_binding_without_repeating_transition(self):
        with self.assertRaises(RuntimeError):
            with self.intent():
                raise RuntimeError("synthetic external outcome unknown")
        before = (self.guard.root / "state.json").read_bytes()
        with patch.object(self.guard, "save", side_effect=AssertionError("replayed transition")):
            with self.intent() as result:
                self.assertTrue(result["reconciliation_required"])
        for kwargs in ({"candidate": "c" * 64}, {"backup": "c" * 64}, {"generation": 2},
                       {"operation": "00000000-0000-4000-8000-000000000002"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(module.PanelRequestError):
                with self.intent(**kwargs):
                    self.fail("conflicting installation entered")
        self.assertEqual((self.guard.root / "state.json").read_bytes(), before)

    def test_uncertain_panel_request_prevents_installation_without_clearing_it(self):
        self.maintenance("cancel")
        with self.assertRaises(module.PanelRequestError):
            self.guard.execute("POST", "/panel/api/clients/add", "fixture", {},
                               lambda: (_ for _ in ()).throw(TimeoutError()))
        with module.maintenance_node_lock(self.lock), self.guard.locked():
            self.guard.maintenance("prepare", OP, 2)
        before = (self.guard.root / "state.json").read_bytes()
        with self.assertRaisesRegex(module.PanelRequestError, "REQUEST_RECONCILIATION_REQUIRED"):
            with self.intent(generation=2):
                self.fail("entered with pending panel request")
        self.assertEqual((self.guard.root / "state.json").read_bytes(), before)

    def test_no_hold_or_cancelled_hold_cannot_enter(self):
        self.maintenance("cancel")
        with self.assertRaises(module.PanelRequestError):
            with self.intent():
                self.fail("cancelled hold entered")
        other = module.PanelRequestGuard(self.root / "absent")
        with self.assertRaises(module.PanelRequestError):
            with other.installation_intent(OP, 1, CANDIDATE, BACKUP, self.lock):
                self.fail("absent hold entered")

    def test_accepted_request_history_survives_and_busy_node_never_enters(self):
        self.maintenance("cancel")
        self.guard.execute("POST", "/panel/api/clients/add", "fixture", {},
                           lambda: (200, b'{"success":true}'))
        previous = self.read()["request"]
        with module.maintenance_node_lock(self.lock), self.guard.locked():
            self.guard.maintenance("prepare", OP, 2)
        before = (self.guard.root / "state.json").read_bytes()
        with module.maintenance_node_lock(self.lock), self.assertRaises(module.PanelRequestError):
            with self.intent(generation=2):
                self.fail("entered while another Node operation held lock")
        self.assertEqual((self.guard.root / "state.json").read_bytes(), before)
        with self.intent(generation=2):
            pass
        self.assertEqual(self.read()["request"], previous)

    def test_failed_file_sync_or_replace_never_enters_external_body(self):
        original_sync = os.fsync

        def sync(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("synthetic file sync failure")
            return original_sync(fd)

        for target, replacement in (("fsync", sync), ("replace", OSError("synthetic replace failure"))):
            before = (self.guard.root / "state.json").read_bytes()
            with patch.object(module.os, target, side_effect=replacement), self.assertRaises(OSError):
                with self.intent():
                    self.fail("external effect before durable intent")
            self.assertEqual((self.guard.root / "state.json").read_bytes(), before)

    def test_post_replace_sync_failure_is_non_cancellable_and_replay_requires_reconciliation(self):
        original_sync = os.fsync

        def sync(fd):
            state = json.loads((self.guard.root / "state.json").read_text())
            if stat.S_ISDIR(os.fstat(fd).st_mode) and state["schema_version"] == 3:
                raise OSError("synthetic directory sync failure")
            return original_sync(fd)

        with patch.object(module.os, "fsync", side_effect=sync), self.assertRaises(OSError):
            with self.intent():
                self.fail("entered after incomplete persistence")
        with self.assertRaises(module.PanelRequestError):
            self.maintenance("cancel")
        with self.intent() as result:
            self.assertTrue(result["reconciliation_required"])

    def test_process_death_after_intent_retains_exclusion(self):
        code = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from panel_request_guard import PanelRequestGuard
with PanelRequestGuard().installation_intent(sys.argv[3], 1, "a"*64, "b"*64, Path(sys.argv[2])):
    print("READY", flush=True)
    sys.stdin.read()
'''
        child = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "agent"), str(self.lock), OP],
                                 env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        timer = threading.Timer(10, child.kill)
        timer.start()
        try:
            self.assertEqual(child.stdout.readline().strip(), "READY")
        finally:
            timer.cancel()
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.assertEqual(self.read()["schema_version"], 3)
        with self.assertRaises(module.PanelRequestError):
            self.maintenance("cancel")
        with self.intent() as result:
            self.assertTrue(result["reconciliation_required"])

    def test_corrupt_installation_state_never_allows_writes(self):
        with self.intent():
            pass
        valid = self.read()
        mutations = []
        for key in ("installation", "request", "maintenance"):
            case = copy.deepcopy(valid)
            del case[key]
            mutations.append(case)
        for field, value in (("phase", "DONE"), ("candidate_sha256", "bad"), ("extra", True)):
            case = copy.deepcopy(valid)
            case["installation"][field] = value
            mutations.append(case)
        case = copy.deepcopy(valid)
        case["maintenance"]["phase"] = "CANCELLED"
        mutations.append(case)
        for case in mutations:
            with self.guard.locked():
                self.guard.save(case)
            with self.assertRaises(module.PanelRequestError):
                self.guard.execute("POST", "/panel/api/clients/add", "fixture", {},
                                   lambda: self.fail("corrupt state dispatched"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
