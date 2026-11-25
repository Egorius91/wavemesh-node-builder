"""Disposable Linux/root journal proof; never run against an installed run.

The timestamps and server receipts here are synthetic. Actual filesystem,
flock, nofollow, fsync calls and child-exit custody are exercised on Linux.
This suite does not prove NTP, managed service admission or a 24-hour interval.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'agent'))
sys.path.insert(0, str(ROOT/'tests/unit'))
import managed_observation as M
from test_managed_observation import manifest, event


class PosixManagedObservationJournal(unittest.TestCase):
    def setUp(self):
        if os.name != 'posix' or os.geteuid() != 0:
            raise RuntimeError('NATIVE_LINUX_ROOT_REQUIRED_NOT_A_SKIP')
        self.parent = Path(os.environ.get('PR8_NATIVE_TMPDIR','/var/tmp'))
        self.assertTrue(self.parent == Path('/var/tmp') or re.fullmatch(
            '/var/tmp/wm-pr8-managed-qualification-[a-f0-9]{12}/native-tmp',str(self.parent)))
        self.assertEqual(self.parent.resolve(),self.parent)
        self.directory = Path(tempfile.mkdtemp(prefix='wm-pr8-journal-', dir=self.parent))
        self.assertEqual(self.directory.parent,self.parent)
        self.directory.chmod(0o700)
        self.history = self.directory/'history'
        self.history.mkdir(mode=0o700)
        self.base = datetime.now(timezone.utc)-timedelta(seconds=10)
        self.manifest = manifest()
        self.manifest.update(not_before=M.stamp(self.base-timedelta(seconds=10)),
            stop_at=M.stamp(self.base+timedelta(seconds=60)),
            human_window_stop_at=M.stamp(self.base+timedelta(seconds=120)),
            required_seconds=10, cadence_seconds=5, max_gap_seconds=15,
            final_readback_seconds=60)
        self.sha = hashlib.sha256(M.encoded(self.manifest)+b'\n').hexdigest()
        self.journals = []

    def tearDown(self):
        for journal in self.journals:
            journal.close()
        resolved = self.directory.resolve()
        self.assertEqual(resolved.parent,self.parent)
        self.assertTrue(resolved.name.startswith('wm-pr8-journal-'))
        shutil.rmtree(resolved)

    def writer(self):
        journal = M.RunJournal(self.history, self.manifest, self.sha)
        self.journals.append(journal)
        return journal

    def cycle(self, seconds=0):
        value = event(seconds)
        at = self.base+timedelta(seconds=seconds)
        value.update(started_at=M.stamp(at-timedelta(seconds=1)), at=M.stamp(at),
            request_sent_at=M.stamp(at-timedelta(seconds=1)))
        value['receipt'].update(received_at=value['at'], request_sent_at=value['request_sent_at'],
            credential_not_before=M.stamp(at-timedelta(hours=1)),
            credential_expires_at=M.stamp(at+timedelta(hours=1)),
            credential_acknowledged_at=M.stamp(at-timedelta(minutes=1)))
        return value

    def append(self, journal, value):
        journal.begin_attempt(value['request_sent_at'])
        return journal.append(value)

    def test_real_roundtrip_retains_lost_reply_gap_and_paired_hashes(self):
        writer = self.writer()
        self.append(writer, self.cycle())
        lost = self.cycle(5)
        lost.update(receipt=None, transport_succeeded=False)
        self.append(writer, lost)
        self.append(writer, self.cycle(10))
        samples, tip, used = M.read_history(self.history, self.manifest, self.sha)
        self.assertEqual([s['result'] for s in samples], ['QUALIFYING','GAP','QUALIFYING'])
        self.assertNotEqual(samples[0]['segment'], samples[-1]['segment'])
        self.assertEqual(tip, writer.tip)
        self.assertEqual(used, writer.bytes)
        self.assertEqual(stat.S_IMODE((self.history/'00000001.json').stat().st_mode), 0o600)

    def test_second_open_file_description_cannot_acquire_writer_flock(self):
        writer = self.writer()
        with self.assertRaises(BlockingIOError):
            M.RunJournal(self.history, self.manifest, self.sha)
        self.assertEqual(writer.samples, [])
        self.assertEqual(sorted(p.name for p in self.history.iterdir()), ['writer.lock'])

    def test_exclusive_sample_collision_keeps_original_bytes_and_blocks_retry(self):
        writer = self.writer()
        value = self.cycle()
        writer.begin_attempt(value['request_sent_at'])
        target = self.history/'00000001.json'
        target.write_bytes(b'partial-original-evidence')
        target.chmod(0o600)
        with self.assertRaises(FileExistsError):
            writer.append(value)
        self.assertTrue(writer.uncertain)
        with self.assertRaisesRegex(M.ObservationError, 'JOURNAL_BLOCKED'):
            writer.begin_attempt(value['request_sent_at'])
        self.assertEqual(target.read_bytes(), b'partial-original-evidence')
        with self.assertRaises((M.ObservationError, json.JSONDecodeError)):
            M.read_history(self.history, self.manifest, self.sha)

    def test_directory_fsync_fault_keeps_original_attempt_and_never_retries(self):
        writer = self.writer()
        with patch.object(M, 'fsync_directory', side_effect=OSError('synthetic fsync fault')):
            with self.assertRaises(OSError):
                writer.begin_attempt(self.cycle()['request_sent_at'])
        before = (self.history/'attempt-00000001.json').read_bytes()
        self.assertTrue(writer.uncertain)
        with self.assertRaisesRegex(M.ObservationError, 'JOURNAL_BLOCKED'):
            writer.begin_attempt(self.cycle()['request_sent_at'])
        self.assertEqual((self.history/'attempt-00000001.json').read_bytes(), before)
        writer.close()
        with self.assertRaisesRegex(M.ObservationError, 'HISTORY_UNRESOLVED_ATTEMPT_OR_LIMIT'):
            self.writer()

    def test_sample_fsync_fault_cannot_be_hidden_by_terminal_receipt(self):
        writer = self.writer()
        value = self.cycle()
        writer.begin_attempt(value['request_sent_at'])
        with patch.object(M, 'fsync_directory', side_effect=OSError('synthetic fsync fault')):
            with self.assertRaises(OSError):
                writer.append(value)
        self.assertTrue((self.history/'00000001.json').exists())
        writer.terminal('JOURNAL_UNCERTAIN')
        with self.assertRaisesRegex(M.ObservationError, 'TERMINAL_HISTORY_BINDING'):
            M.read_history(self.history, self.manifest, self.sha)

    def test_lock_inode_replacement_refuses_before_another_attempt(self):
        writer = self.writer()
        lock = self.history/'writer.lock'
        lock.rename(self.directory/'original-writer.lock')
        lock.touch(mode=0o600)
        with self.assertRaisesRegex(M.ObservationError, 'JOURNAL_OR_LOCK_REPLACED'):
            writer.begin_attempt(self.cycle()['request_sent_at'])
        self.assertFalse((self.history/'attempt-00000001.json').exists())

    def test_history_directory_replacement_refuses_even_with_copied_lock(self):
        writer = self.writer()
        self.history.rename(self.directory/'original-history')
        self.history.mkdir(mode=0o700)
        (self.history/'writer.lock').touch(mode=0o600)
        with self.assertRaisesRegex(M.ObservationError, 'JOURNAL_OR_LOCK_REPLACED'):
            writer.begin_attempt(self.cycle()['request_sent_at'])
        self.assertFalse((self.history/'attempt-00000001.json').exists())

    def test_symlink_hardlink_fifo_and_public_permissions_are_refused(self):
        source = self.directory/'private.json'
        source.write_bytes(b'{}'); source.chmod(0o600)
        link = self.directory/'symlink.json'; link.symlink_to(source)
        with self.assertRaisesRegex(M.ObservationError, 'UNSAFE_PATH'):
            M.read_bytes(link, 1024)
        hard = self.directory/'hardlink.json'; os.link(source, hard)
        with self.assertRaisesRegex(M.ObservationError, 'UNSAFE_FILE'):
            M.read_bytes(source, 1024)
        hard.unlink(); source.chmod(0o644)
        with self.assertRaisesRegex(M.ObservationError, 'UNSAFE_FILE'):
            M.read_bytes(source, 1024)
        fifo = self.directory/'fifo'; os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(M.ObservationError, 'UNSAFE_FILE'):
            M.read_bytes(fifo, 1024)

    def test_real_child_exit_after_intent_retains_an_unresolved_original(self):
        packet = self.directory/'synthetic-manifest.json'
        packet.write_text(json.dumps(self.manifest)); packet.chmod(0o600)
        code = ("import json,os,sys;from pathlib import Path;"
            "sys.path.insert(0,sys.argv[1]);import managed_observation as m;"
            "j=m.RunJournal(Path(sys.argv[2]),json.loads(Path(sys.argv[3]).read_text()),sys.argv[4]);"
            "j.begin_attempt(sys.argv[5]);os._exit(9)")
        process = subprocess.Popen([sys.executable,'-B','-c',code,str(ROOT/'agent'),
            str(self.history),str(packet),self.sha,self.cycle()['request_sent_at']],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual((process.returncode,stdout,stderr), (9,b'',b''))
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=3)
            process.stdout.close(); process.stderr.close()
        self.assertTrue((self.history/'attempt-00000001.json').exists())
        self.assertFalse((self.history/'00000001.json').exists())
        with self.assertRaisesRegex(M.ObservationError, 'HISTORY_UNRESOLVED_ATTEMPT_OR_LIMIT'):
            self.writer()

    def test_retained_terminal_cannot_resume_and_history_tampering_refuses(self):
        writer = self.writer()
        self.append(writer, self.cycle())
        writer.terminal('INTERRUPTED')
        writer.close()
        with self.assertRaisesRegex(M.ObservationError, 'RETAINED_TERMINAL_RUN_NO_RESUME'):
            self.writer()
        target = self.history/'00000001.json'
        value = json.loads(target.read_bytes()); value['local']['health_healthy'] = False
        target.write_bytes(M.encoded(value)+b'\n')
        with self.assertRaisesRegex(M.ObservationError, 'SAMPLE_LINEAGE_OR_DECISION'):
            M.read_history(self.history, self.manifest, self.sha)

    def test_sample_limit_reserves_control_storage_and_expired_window_never_writes(self):
        self.manifest['max_samples'] = 2
        writer = self.writer()
        self.append(writer, self.cycle()); self.append(writer, self.cycle(5))
        with self.assertRaisesRegex(M.ObservationError, 'JOURNAL_LIMIT'):
            writer.begin_attempt(self.cycle(10)['request_sent_at'])
        self.assertFalse((self.history/'attempt-00000003.json').exists())
        writer.terminal('JOURNAL_UNCERTAIN')
        self.assertLess(writer.bytes+M.MAX_SAMPLE, self.manifest['max_bytes'])
        self.manifest['human_window_stop_at'] = M.stamp(datetime.now(timezone.utc)-timedelta(seconds=1))
        with self.assertRaisesRegex(M.ObservationError, 'HUMAN_WINDOW_EXPIRED_NO_WRITE'):
            writer.terminal('DEADLINE', stopped=True)
        self.assertFalse((self.history/'STOPPED.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
