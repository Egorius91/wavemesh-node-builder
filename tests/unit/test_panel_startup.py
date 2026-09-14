"""Root/POSIX startup checks run in the dedicated systemd CI workflow."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
from panel_request_guard import PanelRequestGuard, PanelRequestError, maintenance_node_lock

OP = "00000000-0000-4000-8000-000000000001"
ACCEPTED = {"schema_version": 1, "phase": "RESPONSE_ACCEPTED",
            "attempt_id": "a" * 64, "request_digest": "b" * 64}


@unittest.skipUnless(sys.platform == "linux" and getattr(os, "geteuid", lambda: -1)() == 0,
                     "dedicated root Linux CI")
class StartupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wm-startup-unit-", dir="/run")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.guard = PanelRequestGuard(self.root / "journal")
        self.lock = self.root / "node.lock"

    def save(self, value):
        with self.guard.locked():
            self.guard.save(value)

    def check(self):
        self.guard.check_startup(self.lock)

    def test_missing_root_and_state_fail_without_initialization(self):
        with self.assertRaises(OSError):
            self.check()
        self.assertFalse(self.guard.root.exists())
        with self.guard.locked():
            pass
        with self.assertRaises(PanelRequestError):
            self.check()
        self.assertFalse((self.guard.root / "state.json").exists())

    def test_only_accepted_and_cancelled_allow_without_changing_state(self):
        self.save(ACCEPTED)
        before = (self.guard.root / "state.json").read_bytes()
        self.check()
        self.assertEqual((self.guard.root / "state.json").read_bytes(), before)
        pending = {**ACCEPTED, "phase": "DISPATCH_INTENT"}
        self.save(pending)
        with self.assertRaises(PanelRequestError):
            self.check()
        self.save(ACCEPTED)
        with maintenance_node_lock(self.lock), self.guard.locked():
            self.guard.maintenance("prepare", OP, 1)
        with self.assertRaises(PanelRequestError):
            self.check()
        with maintenance_node_lock(self.lock), self.guard.locked():
            self.guard.maintenance("cancel", OP, 1)
        self.check()

    def test_installation_survives_loss_of_volatile_lock(self):
        with maintenance_node_lock(self.lock), self.guard.locked():
            self.guard.maintenance("prepare", OP, 1)
        with self.guard.installation_intent(OP, 1, "a" * 64, "b" * 64, self.lock):
            pass
        before = (self.guard.root / "state.json").read_bytes()
        for _ in range(2):
            self.lock.unlink()
            with self.assertRaises(PanelRequestError):
                self.check()
            self.assertEqual((self.guard.root / "state.json").read_bytes(), before)

    def test_busy_node_and_journal_locks_deny(self):
        self.save(ACCEPTED)
        with maintenance_node_lock(self.lock), self.assertRaises(PanelRequestError):
            self.check()
        with self.guard.locked(), self.assertRaises(PanelRequestError):
            self.check()

    def test_corruption_and_unsafe_storage_deny(self):
        self.save(ACCEPTED)
        path = self.guard.root / "state.json"
        for raw in (b'{', b'{"schema_version":1,"schema_version":1}', b'x' * 4097):
            path.write_bytes(raw)
            with self.assertRaises(PanelRequestError):
                self.check()
        self.save(ACCEPTED)
        self.root.chmod(0o777)
        try:
            with self.assertRaises(PanelRequestError):
                self.check()
        finally:
            self.root.chmod(0o700)
        path.chmod(0o644)
        with self.assertRaises(PanelRequestError):
            self.check()
        path.chmod(0o600)
        saved = self.root / "saved"
        path.rename(saved)
        path.symlink_to(saved)
        with self.assertRaises(OSError):
            self.check()


if __name__ == "__main__":
    unittest.main()
