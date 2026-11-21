"""Fresh offline Linux guest only: real FD9/flock and filesystem barriers.

Collector remains an explicit synthetic fixture here; this is NOT full Producer
composition. Select this class separately from portable tests, with no skips.
"""
import fcntl
import os
from pathlib import Path
import sys
import unittest

from tests.unit import test_prepared_intent as portable


class NativePreparedIntent(portable.PreparedIntent):
    def setUp(self):
        if not sys.platform.startswith("linux") or os.geteuid() != 0:
            raise RuntimeError("fresh isolated root Linux guest required")
        try:
            os.fstat(9)
        except OSError:
            pass
        else:
            raise RuntimeError("FD9 already owned; do not replace it")
        super().setUp()
        self.lock = portable.completion.NODE_MUTATION_LOCK
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        if self.fd == 9:
            self.fd = os.dup(self.fd)
        os.dup2(self.fd, 9)
        self.addCleanup(self.release)
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self):
        os.close(9)
        os.close(self.fd)
        self.lock.unlink()

    def prepare(self, *, real_owner=True):
        return super().prepare(real_owner=real_owner)

    def test_wrong_lock_inode_refuses_with_real_fd9(self):
        wrong = os.open(self.base / "wrong-lock", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            os.dup2(wrong, 9)
            with self.assertRaises(portable.writer.IntentUnavailable): self.prepare()
            self.assertEqual({p.name for p in self.directory.iterdir()}, {"plan.json", "result.json"})
        finally:
            os.dup2(self.fd, 9)
            os.close(wrong)


if __name__ == "__main__":
    unittest.main()
