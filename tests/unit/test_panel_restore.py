#!/usr/bin/env python3
import importlib.util
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts/lib/panel_restore.py"
spec = importlib.util.spec_from_file_location("panel_restore", TOOL)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
STOPPED = "\n".join(["LoadState=loaded", "ActiveState=inactive", "SubState=dead",
                     "MainPID=0", "ControlPID=0", "ControlGroup=",
                     "KillMode=control-group", "SendSIGKILL=yes"])


def database(path, value):
    with closing(sqlite3.connect(path)) as db:
        db.execute("create table data(value text)")
        db.execute("insert into data values (?)", (value,))
        db.commit()


def value(path):
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("pragma integrity_check").fetchall() == [("ok",)]
        return db.execute("select value from data").fetchone()[0]


class PanelRestoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.target = self.root / "snapshot.db", self.root / "live.db"
        database(self.source, "snapshot")
        database(self.target, "current")

    def test_stopped_state_must_be_complete_and_unambiguous(self):
        module.verify_stopped(STOPPED)
        for before, after in [("inactive", "active"), ("inactive", "deactivating"),
                              ("dead", "running"), ("MainPID=0", "MainPID=123"),
                              ("ControlPID=0", "ControlPID=456"),
                              ("KillMode=control-group", "KillMode=process"),
                              ("SendSIGKILL=yes", "SendSIGKILL=no"),
                              ("LoadState=loaded", "LoadState=not-found")]:
            with self.subTest(after=after), self.assertRaises(ValueError):
                module.verify_stopped(STOPPED.replace(before, after))
        for text in ("", STOPPED + "\nMainPID=0", STOPPED.replace("ControlGroup=\n", ""),
                     STOPPED + "\nunknown=value"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                module.verify_stopped(text)

    def test_nonempty_cgroup_requires_recursive_population_zero(self):
        state = STOPPED.replace("ControlGroup=", "ControlGroup=/system.slice/x-ui.service")
        folder = self.root / "system.slice/x-ui.service"
        folder.mkdir(parents=True)
        for content in ("populated 1\nfrozen 0\n", "", "populated 0\npopulated 0\n"):
            (folder / "cgroup.events").write_text(content)
            with self.assertRaises(ValueError):
                module.verify_stopped(state, self.root)
        (folder / "cgroup.events").write_text("populated 0\nfrozen 0\n")
        module.verify_stopped(state, self.root)
        (folder / "cgroup.events").unlink()
        with self.assertRaises(OSError):
            module.verify_stopped(state, self.root)
        with self.assertRaises(ValueError):
            module.verify_stopped(state.replace("/system.slice/x-ui.service", "/../escape"), self.root)

    def test_restores_database_and_repeat_preserves_snapshot(self):
        snapshot = self.source.read_bytes()
        module.restore(self.source, self.target)
        module.restore(self.source, self.target)
        self.assertEqual(value(self.target), "snapshot")
        self.assertEqual(self.source.read_bytes(), snapshot)

    def test_crashed_writer_wal_is_not_replayed_over_restored_contents(self):
        code = """import sqlite3,os,sys
db=sqlite3.connect(sys.argv[1]); db.execute('pragma journal_mode=WAL')
db.execute('pragma wal_autocheckpoint=0')
db.execute("update data set value='crashed-writer'"); db.commit(); os._exit(0)
"""
        subprocess.run([sys.executable, "-c", code, str(self.target)], check=True, timeout=10)
        self.assertGreater(Path(str(self.target) + "-wal").stat().st_size, 0)
        module.restore(self.source, self.target)
        self.assertEqual(value(self.target), "snapshot")
        self.assertEqual(value(self.source), "snapshot")

    def test_corrupt_backup_is_rejected_before_target_change(self):
        before = self.target.read_bytes()
        self.source.write_bytes(b"not a sqlite database")
        with self.assertRaises((sqlite3.Error, ValueError)):
            module.restore(self.source, self.target)
        self.assertEqual(before, self.target.read_bytes())

    def test_empty_snapshot_cannot_erase_application_database(self):
        before = self.target.read_bytes()
        self.source.write_bytes(b"")
        with self.assertRaises(ValueError):
            module.restore(self.source, self.target)
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("VACUUM")
        with self.assertRaises(ValueError):
            module.restore(self.source, self.target)
        self.assertEqual(before, self.target.read_bytes())

    def test_lock_wait_is_bounded_and_original_survives(self):
        lock = sqlite3.connect(self.target)
        try:
            lock.execute("begin immediate")
            started = time.monotonic()
            with self.assertRaises((TimeoutError, sqlite3.OperationalError)):
                module.restore(self.source, self.target, timeout=0.2)
            self.assertLess(time.monotonic() - started, 2)
        finally:
            lock.rollback()
            lock.close()
        self.assertEqual(value(self.target), "current")

    def test_abort_during_incremental_copy_rolls_back_target(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("create table padding(value blob)")
            db.executemany("insert into padding values (?)", [(bytes(4096),)] * 500)
            db.commit()
        with patch.object(module.time, "monotonic", side_effect=[0.0, 1.0]):
            with self.assertRaises(TimeoutError):
                module.restore(self.source, self.target, timeout=0.2)
        self.assertEqual(value(self.target), "current")
        self.assertEqual(value(self.source), "snapshot")

    def test_missing_target_or_snapshot_sidecar_rejected(self):
        with self.assertRaises(OSError):
            module.restore(self.source, self.root / "missing.db")
        Path(str(self.source) + "-wal").write_bytes(b"pending")
        with self.assertRaises(ValueError):
            module.restore(self.source, self.target)
        self.assertEqual(value(self.target), "current")

    def test_process_death_during_restore_keeps_recoverable_original(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("create table padding(value blob)")
            db.executemany("insert into padding values (?)", [(bytes(4096),)] * 2000)
            db.commit()
        marker = self.root / "copy-in-progress"
        code = r'''
import importlib.util,sqlite3,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location("restore",sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
connect=sqlite3.connect
class Paused(sqlite3.Connection):
    def backup(self,target,**options):
        original=options["progress"]
        def progress(status,remaining,total):
            if 0 < remaining < total//2:
                Path(sys.argv[4]).write_text("COPY_IN_PROGRESS")
                time.sleep(30)
            original(status,remaining,total)
        options["progress"]=progress
        return super().backup(target,**options)
m.sqlite3.connect=lambda *args,**kwargs: connect(*args,factory=Paused,**kwargs)
m.restore(sys.argv[2],sys.argv[3])
'''
        process = subprocess.Popen([sys.executable, "-c", code, str(TOOL), str(self.source),
                                    str(self.target), str(marker)], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "child did not reach partial restore")
            self.assertIsNone(process.poll())
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.assertEqual(value(self.target), "current")
        self.assertEqual(value(self.source), "snapshot")
        module.restore(self.source, self.target)
        self.assertEqual(value(self.target), "snapshot")

    @unittest.skipUnless(os.name == "posix", "POSIX link/ownership checks")
    def test_symlinks_and_hardlinks_rejected(self):
        linked = self.root / "linked.db"
        linked.symlink_to(self.source)
        with self.assertRaises(ValueError):
            module.restore(linked, self.target)
        linked.unlink()
        os.link(self.source, linked)
        with self.assertRaises(ValueError):
            module.restore(linked, self.target)
        linked.unlink()
        side = Path(str(self.target) + "-wal")
        side.symlink_to(self.source)
        with self.assertRaises(ValueError):
            module.restore(self.source, self.target)


@unittest.skipUnless(os.name == "posix", "real Bash rollback integration runs in Linux CI")
class ShellRollbackTest(unittest.TestCase):
    def test_stop_restore_and_start_failures_prevent_later_effects(self):
        for failure in ("stop", "active", "show", "corrupt", "start", "config", "success"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                database(root / "snapshot.db", "snapshot")
                database(root / "live.db", "current")
                transaction = root / "transaction"
                transaction.mkdir()
                (transaction / "subscriptions.before.absent").touch()
                (root / "snapshot.db").replace(transaction / "x-ui.before.db")
                (transaction / "x-ui.before.db.path").write_text(str(root / "live.db"))
                if failure == "corrupt":
                    (transaction / "x-ui.before.db").write_bytes(b"invalid")
                (transaction / "config.before.json").write_text('{"version":"old"}')
                (root / "config.json").write_text('{"version":"current"}')
                script = r'''
set -Eeuo pipefail
WM_STATE_DIR="$CASE_DIR"
WM_CONFIG_JSON="$CASE_DIR/config.json"
WM_RUNTIME_JSON="$CASE_DIR/runtime.json"
WM_SUB_DIR="$CASE_DIR/subs"
WM_NGINX_MANAGED_CONF="$CASE_DIR/nginx"
source "$TRANSACTION_SOURCE"
wm_warn() { :; }
wm_transaction_panel_admission() { return 0; }
wm_load_config() { [[ "$FAILURE" != config ]]; }
wm_transaction_wait_xui() { return 0; }
wm_transaction_post_rollback_check() { return 0; }
nginx() { echo nginx >> "$CASE_DIR/effects"; }
systemctl() {
  echo "$1" >> "$CASE_DIR/effects"
  case "$1" in
    stop) [[ "$FAILURE" != stop ]];;
    start) [[ "$FAILURE" != start ]];;
    show)
      [[ "$FAILURE" != show ]] || return 1
      state=inactive; [[ "$FAILURE" != active ]] || state=active
      printf '%s\n' LoadState=loaded "ActiveState=$state" SubState=dead MainPID=0 ControlPID=0 ControlGroup= KillMode=control-group SendSIGKILL=yes;;
  esac
}
if wm_transaction_rollback "$CASE_DIR/transaction" test; then exit 0; else exit 9; fi
'''
                env = dict(os.environ, CASE_DIR=str(root), FAILURE=failure,
                           TRANSACTION_SOURCE=str(ROOT / "scripts/lib/transaction.sh"))
                result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, timeout=20)
                expected = 0 if failure == "success" else 9
                self.assertEqual(result.returncode, expected, result.stderr.decode())
                import json
                status = json.loads((transaction / "result.json").read_text())["status"]
                self.assertEqual(status, "rolled_back" if failure == "success" else "rollback_failed")
                effects = (root / "effects").read_text().splitlines()
                if failure in ("stop", "active", "show", "corrupt"):
                    self.assertNotIn("start", effects)
                    self.assertIn("current", (root / "config.json").read_text())
                    self.assertEqual(value(root / "live.db"), "current")
                if failure != "success":
                    self.assertNotIn("nginx", effects)
                    self.assertTrue((transaction / "x-ui.before.db").exists())
                else:
                    self.assertEqual(value(root / "live.db"), "snapshot")
                    self.assertIn("old", (root / "config.json").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
