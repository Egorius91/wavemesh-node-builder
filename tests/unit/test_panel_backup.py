import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent'))
import panel_backup as module
import panel_request_guard as journal

OP = '00000000-0000-4000-8000-000000000001'
CANDIDATE = 'a' * 64


class ManifestTest(unittest.TestCase):
    def manifest(self):
        return {'schema': 1, 'phase': 'SEALED_SNAPSHOT',
                'intent': {'operation_id': OP, 'generation': 1, 'candidate_sha256': CANDIDATE,
                           'nonce': 'b'*64, 'panel_home': str(module.PANEL_HOME), 'panel_db': str(module.PANEL_DB)},
                'panel': {'x-ui': {'kind': 'file', 'mode': 0o755, 'size': 1, 'sha256': 'c'*64}},
                'database': {'size': 4096, 'sha256': 'd'*64}}

    def test_manifest_rejects_paths_modes_types_and_unknown_fields(self):
        value = self.manifest()
        module.validate_manifest(value)
        bad = []
        for key in value:
            bad.append({**value, key: None})
        bad.append({**value, 'restore_authorized': True})
        for name in ('../x', '/x', '.', 'x/../y', 'x//y', 'x\\y', ''):
            bad.append({**value, 'panel': {name: value['panel']['x-ui']}})
        for key, replacement in (('mode', 0o777), ('mode', 0o4755), ('mode', True),
                                 ('size', -1), ('size', True), ('sha256', 'bad')):
            bad.append({**value, 'panel': {'x-ui': {**value['panel']['x-ui'], key: replacement}}})
        for key, replacement in (('generation', True), ('candidate_sha256', 'x'*64),
                                 ('panel_db', '/untrusted.db'), ('nonce', ''), ('operation_id', 'bad')):
            bad.append({**value, 'intent': {**value['intent'], key: replacement}})
        for item in bad:
            with self.subTest(item=item), self.assertRaises((module.BackupError, journal.PanelRequestError)):
                module.validate_manifest(item)


@unittest.skipUnless(sys.platform == 'linux' and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'dedicated root Linux CI')
class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wm-backup-', dir='/run')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'panel'
        self.home.mkdir(mode=0o755)
        (self.home / 'bin').mkdir(mode=0o755)
        (self.home / 'x-ui').write_bytes(b'original executable')
        (self.home / 'x-ui').chmod(0o755)
        (self.home / 'bin' / 'config.json').write_bytes(b'{"private":"fixture"}')
        (self.home / 'bin' / 'config.json').chmod(0o600)
        self.source = self.root / 'live.sqlite'
        self.db = sqlite3.connect(self.source)
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA wal_autocheckpoint=0')
        self.db.execute('CREATE TABLE clients (id INTEGER PRIMARY KEY, enabled INTEGER)')
        self.db.commit()
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        # This committed row exists only in WAL until the connection closes.
        self.db.execute('INSERT INTO clients VALUES (1, 0)')
        self.db.commit()
        self.guard = journal.PanelRequestGuard(self.root / 'journal')
        self.lock = self.root / 'node.lock'
        self.addCleanup(patch.stopall)
        patch.object(journal, 'DEFAULT_ROOT', self.guard.root).start()
        patch.object(module, 'PANEL_HOME', self.home).start()
        patch.object(module, 'PANEL_DB', self.source).start()
        with journal.maintenance_node_lock(self.lock), self.guard.locked():
            self.guard.maintenance('prepare', OP, 1)
        self.snapshot = module.location(self.guard, OP, 1)

    def prepare(self, candidate=CANDIDATE):
        return module.prepare(self.guard, OP, 1, candidate, self.lock)

    def manifest(self):
        return json.loads((self.snapshot / 'manifest.json').read_bytes())

    def test_online_wal_snapshot_is_standalone_and_captures_committed_rows(self):
        raw_copy = self.root / 'raw.sqlite'
        raw_copy.write_bytes(self.source.read_bytes())
        with sqlite3.connect(raw_copy.as_uri() + '?immutable=1', uri=True) as raw:
            self.assertEqual(raw.execute('SELECT * FROM clients').fetchall(), [])
        result = self.prepare()
        self.assertFalse(result['reconciliation_required'])
        with sqlite3.connect((self.snapshot / 'database.sqlite').as_uri() + '?immutable=1', uri=True) as db:
            self.assertEqual(db.execute('SELECT * FROM clients').fetchall(), [(1, 0)])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertEqual({p.name for p in self.snapshot.iterdir()},
                         {'intent.json', 'manifest.json', 'panel', 'database.sqlite'})
        manifest = self.manifest()
        self.assertEqual(manifest['panel']['x-ui']['mode'], 0o755)
        self.assertEqual((self.snapshot / 'panel/x-ui').read_bytes(), (self.home / 'x-ui').read_bytes())
        for item in self.snapshot.rglob('*'):
            self.assertEqual(item.stat().st_mode & 0o777, 0o700 if item.is_dir() else 0o600)
        self.assertEqual(result['rollback_manifest_sha256'],
                         hashlib.sha256((self.snapshot / 'manifest.json').read_bytes()).hexdigest())

    def test_lost_result_replay_verifies_old_snapshot_without_recapturing(self):
        result = self.prepare()
        self.db.execute('INSERT INTO clients VALUES (2, 1)'); self.db.commit()
        with patch.object(module, 'snapshot_database', side_effect=AssertionError('SECOND_COPY')):
            replay = self.prepare()
        self.assertTrue(replay['reconciliation_required'])
        self.assertEqual(result['rollback_manifest_sha256'], replay['rollback_manifest_sha256'])
        with sqlite3.connect(self.snapshot / 'database.sqlite') as db:
            self.assertEqual(db.execute('SELECT * FROM clients').fetchall(), [(1, 0)])
        with self.assertRaisesRegex(module.BackupError, 'BACKUP_BINDING_CHANGED'):
            self.prepare('f'*64)

    def test_snapshot_retains_both_locks_and_does_not_change_guard(self):
        before = (self.guard.root / 'state.json').read_bytes()
        actual = module.snapshot_database
        def locked(*args):
            for lock in (journal.maintenance_node_lock(self.lock), self.guard.locked()):
                with self.assertRaises(journal.PanelRequestError):
                    with lock:
                        self.fail('snapshot did not hold lock')
            return actual(*args)
        with patch.object(module, 'snapshot_database', side_effect=locked):
            self.prepare()
        self.assertEqual((self.guard.root / 'state.json').read_bytes(), before)

    def test_cancelled_or_uncertain_requests_cannot_create_snapshot(self):
        with self.guard.locked():
            held = self.guard.load()
        for value in ({**held, 'maintenance': {**held['maintenance'], 'phase': 'CANCELLED'}},
                      {**held, 'request': {'schema_version': 1, 'phase': 'DISPATCH_INTENT',
                                          'attempt_id': 'b'*64, 'request_digest': 'c'*64}}):
            with self.guard.locked():
                self.guard.save(value)
            with self.assertRaisesRegex(module.BackupError, 'BACKUP_ADMISSION_REQUIRED'):
                self.prepare()
            self.assertFalse(self.snapshot.exists())

    def test_source_links_and_writable_files_fail_closed(self):
        executable = self.home / 'x-ui'
        for variant in ('symlink', 'hardlink', 'writable'):
            with self.subTest(variant=variant):
                if variant == 'symlink':
                    bad = self.home / 'outside'; bad.symlink_to(self.source)
                elif variant == 'hardlink':
                    bad = self.home / 'hardlink'; os.link(executable, bad)
                else:
                    bad = executable; executable.chmod(0o777)
                with self.assertRaises((module.BackupError, OSError)):
                    module.tree(self.home)
                if bad != executable:
                    bad.unlink()
                executable.chmod(0o755)
        sidecar = Path(str(self.source) + '-journal')
        sidecar.symlink_to(self.home / 'x-ui')
        with self.assertRaisesRegex(module.BackupError, 'BACKUP_DATABASE_UNSAFE'):
            self.prepare()
        self.assertFalse((self.snapshot / 'manifest.json').exists())

    def test_process_death_leaves_partial_snapshot_and_never_retries_copy(self):
        import multiprocessing
        def crash():
            with patch.object(module, 'snapshot_database', side_effect=lambda *args: os._exit(86)):
                self.prepare()
        child = multiprocessing.get_context('fork').Process(target=crash)
        child.start(); child.join(timeout=5)
        if child.is_alive():
            child.kill(); child.join(timeout=5)
            self.fail('crash fixture did not terminate')
        self.assertEqual(child.exitcode, 86)
        self.assertTrue((self.snapshot / 'intent.json').exists())
        self.assertFalse((self.snapshot / 'manifest.json').exists())
        with patch.object(module, 'snapshot_database', side_effect=AssertionError('SECOND_COPY')):
            with self.assertRaisesRegex(module.BackupError, 'BACKUP_RECONCILIATION_REQUIRED'):
                self.prepare()

    def test_failed_manifest_publication_is_retained(self):
        with patch.object(module.os, 'rename', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.prepare()
        pending = (self.snapshot / 'manifest.pending').read_bytes()
        with self.assertRaisesRegex(module.BackupError, 'BACKUP_RECONCILIATION_REQUIRED'):
            self.prepare()
        self.assertEqual((self.snapshot / 'manifest.pending').read_bytes(), pending)

    def test_post_publication_sync_failure_reconciles_without_recapture(self):
        actual = module.sync_dir
        def failed(path):
            if path == self.snapshot and (path / 'manifest.json').exists():
                raise OSError('synthetic')
            return actual(path)
        with patch.object(module, 'sync_dir', side_effect=failed):
            with self.assertRaises(OSError):
                self.prepare()
        with patch.object(module, 'snapshot_database', side_effect=AssertionError('SECOND_COPY')):
            self.assertTrue(self.prepare()['reconciliation_required'])

    def test_corrupt_or_extra_snapshot_content_is_never_repaired(self):
        self.prepare()
        file = self.snapshot / 'panel/x-ui'
        original = file.read_bytes()
        file.write_bytes(b'corruption')
        with self.assertRaisesRegex(module.BackupError, 'BACKUP_CONTENT_MISMATCH'):
            self.prepare()
        self.assertEqual(file.read_bytes(), b'corruption')
        file.write_bytes(original)
        (self.snapshot / 'unexpected').write_bytes(b'not ours')
        with self.assertRaisesRegex(module.BackupError, 'BACKUP_RECONCILIATION_REQUIRED'):
            self.prepare()

    def test_installation_binding_rejects_even_self_consistent_manifest_changes(self):
        result = self.prepare()
        with self.guard.installation_intent(OP, 1, CANDIDATE, result['rollback_manifest_sha256'], self.lock):
            pass
        self.assertTrue(self.prepare()['reconciliation_required'])
        manifest = self.manifest()
        manifest['intent']['nonce'] = 'e'*64
        (self.snapshot / 'intent.json').write_text(json.dumps(manifest['intent']))
        (self.snapshot / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(module.BackupError, 'BACKUP_BINDING_CHANGED'):
            self.prepare()

    def test_changed_source_during_capture_and_database_timeout_leave_unsealed_work(self):
        actual = module.snapshot_database
        def change(*args):
            actual(*args)
            (self.home / 'x-ui').write_bytes(b'changed during capture')
        with patch.object(module, 'snapshot_database', side_effect=change):
            with self.assertRaisesRegex(module.BackupError, 'BACKUP_SOURCE_CHANGED'):
                self.prepare()
        self.assertFalse((self.snapshot / 'manifest.json').exists())
        with self.assertRaisesRegex(module.BackupError, 'BACKUP_DATABASE_LIMIT'):
            module.snapshot_database(self.source, self.root / 'timeout.sqlite', timeout=0)


if __name__ == '__main__':
    unittest.main()
