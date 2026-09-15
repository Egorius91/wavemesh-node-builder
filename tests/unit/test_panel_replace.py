from contextlib import closing
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
import panel_replace as module
import panel_backup as storage
import panel_candidate as candidate
import panel_request_guard as journal
import panel_start
import panel_stop
from panel_isolation import IsolationError, PanelIsolation, bound_policy
from test_panel_candidate import fixture, HEAD, OP
from test_panel_stop import FakeStop


class ReplacementContractTest(unittest.TestCase):
    def test_replacement_schema_has_no_start_or_release_alias(self):
        for phase in ('PREPARE_INTENT', 'REPLACE_INTENT', 'REPLACED', 'ROLLBACK_INTENT', 'ROLLED_BACK'):
            value = {'phase': phase, 'manifest_sha256': 'a'*64,
                     'receipt_sha256': '' if phase == 'PREPARE_INTENT' else 'b'*64}
            journal.PanelRequestGuard.validate_replacement(value)
            for key in value:
                for bad in (None, True, [], {}, 'unsupported'):
                    with self.assertRaises(journal.PanelRequestError):
                        journal.PanelRequestGuard.validate_replacement({**value, key: bad})
            with self.assertRaises(journal.PanelRequestError):
                journal.PanelRequestGuard.validate_replacement({**value, 'start_allowed': True})
            with self.assertRaises(journal.PanelRequestError):
                journal.PanelRequestGuard.validate_replacement({**value, 'receipt_sha256': 'b'*64 if phase == 'PREPARE_INTENT' else ''})


@unittest.skipUnless(sys.platform == 'linux' and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'dedicated root Linux CI')
class ReplacementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wm-replace-', dir='/run')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'x-ui'; self.home.mkdir(mode=0o755)
        (self.home / 'bin').mkdir(mode=0o755)
        (self.home / 'x-ui').write_bytes(b'old executable'); (self.home / 'x-ui').chmod(0o755)
        (self.home / 'bin/config.json').write_bytes(b'private original config')
        (self.home / 'bin/config.json').chmod(0o600)
        self.db = self.root / 'x-ui.db'
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('CREATE TABLE clients (id INTEGER PRIMARY KEY, enabled INTEGER)')
            db.execute('INSERT INTO clients VALUES (1,0)'); db.commit()
        self.db.chmod(0o600)
        self.guard = journal.PanelRequestGuard(self.root / 'journal')
        self.lock = self.root / 'node.lock'
        self.addCleanup(patch.stopall)
        patch.object(journal, 'DEFAULT_ROOT', self.guard.root).start()
        patch.object(candidate, 'PANEL_HOME', self.home).start()
        patch.object(storage, 'PANEL_HOME', self.home).start()
        patch.object(storage, 'PANEL_DB', self.db).start()
        with journal.maintenance_node_lock(self.lock), self.guard.locked():
            self.guard.maintenance('prepare', OP, 1)
        bundle = self.root / 'incoming'; bundle.mkdir(mode=0o700)
        value, archive, source = fixture()
        raw = json.dumps(value).encode(); self.manifest = hashlib.sha256(raw).hexdigest()
        for name, data in (('manifest.json', raw), (candidate.ARCHIVE, archive), ('source.tar.gz', source)):
            (bundle / name).write_bytes(data); (bundle / name).chmod(0o600)
        self.candidate = candidate.prepare(self.guard, OP, 1, bundle, self.manifest, HEAD, self.lock)['candidate_sha256']
        self.rollback = storage.prepare(self.guard, OP, 1, self.candidate, self.lock)['rollback_manifest_sha256']
        self.policy = bound_policy(OP, 1, self.candidate, self.rollback, 31333)
        patch.object(PanelIsolation, 'observe', return_value={'nftables': self.policy}).start()
        self.stop = FakeStop(self.guard)
        with self.stop.stopped(self.guard, OP, 1, self.candidate, self.rollback, 31333, 'c'*64, self.lock):
            pass
        patch.object(module, 'PanelStop', return_value=self.stop).start()
        self.controller = module.PanelReplacement()
        self.transaction = module.location(OP, 1)
        self.original = module.describe(self.home)
        self.db_before = self.db.read_bytes()

    def transition(self, action='replace'):
        return self.controller.run(action, self.guard, OP, 1, self.candidate, self.rollback,
                                   self.manifest, HEAD, 31333, 'c'*64, self.lock)

    def phase(self):
        return self.guard.load()['replacement']['phase']

    def assert_start_denied(self):
        with self.assertRaises(journal.PanelRequestError):
            self.guard.check_startup(self.lock)
        start = panel_start.PanelStart()
        with patch.object(start, 'dispatch_start', side_effect=AssertionError('UNSAFE_START')):
            with self.assertRaisesRegex(panel_start.StartError, 'START_STOP_PROOF_REQUIRED'):
                with start.started(self.guard, OP, 1, self.candidate, self.rollback, 31333, 'c'*64, 'd'*64, self.lock):
                    self.fail('replacement admitted startup')
        with self.assertRaisesRegex(panel_stop.StopError, 'STOP_REPLACEMENT_RECONCILIATION_REQUIRED'):
            with self.stop.stopped(self.guard, OP, 1, self.candidate, self.rollback, 31333, 'c'*64, self.lock):
                self.fail('old stop wrapper downgraded replacement state')
        with self.guard.locked():
            with self.assertRaises(journal.PanelRequestError):
                self.guard.maintenance('cancel', OP, 1)
        with self.assertRaises(journal.PanelRequestError):
            self.guard.execute('POST', '/panel/api/clients/add', 'test', {}, lambda: self.fail('unsafe write'))

    def test_real_exchange_and_rollback_restore_original_inode_bytes_and_modes(self):
        self.assertEqual(self.transition()['files'], 'REPLACED')
        self.assertNotEqual(self.home.stat().st_ino, self.original['inode'])
        self.assertEqual((self.home / 'x-ui').read_bytes(), b'fixture x-ui/x-ui')
        self.assertEqual((self.home / 'x-ui').stat().st_mode & 0o777, 0o755)
        self.assertEqual(module.describe(self.transaction / 'slot'), self.original)
        self.assert_start_denied()
        self.assertEqual(self.transition('rollback')['files'], 'ROLLED_BACK')
        self.assertEqual(module.describe(self.home), self.original)
        self.assertEqual(self.db.read_bytes(), self.db_before)
        self.assert_start_denied()
        with patch.object(module, 'exchange', side_effect=AssertionError('SECOND_EXCHANGE')):
            self.assertTrue(self.transition('rollback')['reconciliation_required'])
            with self.assertRaises(module.ReplacementError):
                self.transition()

    def test_lost_results_in_both_directions_reconcile_without_second_exchange(self):
        actual = module.exchange
        def lost(*args):
            actual(*args)
            raise module.ReplacementError('SYNTHETIC_LOST_RESULT')
        for action, pending, terminal in (('replace', 'REPLACE_INTENT', 'REPLACED'),
                                          ('rollback', 'ROLLBACK_INTENT', 'ROLLED_BACK')):
            with patch.object(module, 'exchange', side_effect=lost) as call:
                with self.assertRaisesRegex(module.ReplacementError, 'SYNTHETIC_LOST_RESULT'):
                    self.transition(action)
                self.assertEqual(call.call_count, 1)
            self.assertEqual(self.phase(), pending)
            self.assert_start_denied()
            with patch.object(module, 'exchange', side_effect=AssertionError('SECOND_EXCHANGE')):
                self.assertEqual(self.transition(action)['files'], terminal)
        self.assertEqual(module.describe(self.home), self.original)

    def test_process_death_after_exchange_is_reconciled(self):
        import multiprocessing
        actual = module.exchange
        def crash():
            def die(*args):
                actual(*args); os._exit(86)
            with patch.object(module, 'exchange', side_effect=die):
                self.transition()
        child = multiprocessing.get_context('fork').Process(target=crash)
        child.start(); child.join(timeout=5)
        if child.is_alive():
            child.kill(); child.join(timeout=5); self.fail('crash did not terminate')
        self.assertEqual(child.exitcode, 86)
        self.assertEqual(self.phase(), 'REPLACE_INTENT')
        self.assert_start_denied()
        with patch.object(module, 'exchange', side_effect=AssertionError('SECOND_EXCHANGE')):
            self.assertEqual(self.transition()['files'], 'REPLACED')

    def test_failure_before_exchange_is_not_retried_and_explicit_rollback_accepts_original(self):
        with patch.object(module, 'exchange', side_effect=module.ReplacementError('NO_RESULT')):
            with self.assertRaises(module.ReplacementError):
                self.transition()
        with patch.object(module, 'exchange', side_effect=AssertionError('SECOND_EXCHANGE')):
            with self.assertRaisesRegex(module.ReplacementError, 'REPLACEMENT_RECONCILIATION_REQUIRED'):
                self.transition()
            self.assertEqual(self.transition('rollback')['files'], 'ROLLED_BACK')
        self.assertEqual(module.describe(self.home), self.original)

    def test_failed_activation_copy_retains_prepare_intent_and_blocks_startup(self):
        with patch.object(self.controller, 'activation_tree', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.transition()
        self.assertEqual(self.phase(), 'PREPARE_INTENT')
        self.assert_start_denied()
        with patch.object(self.controller, 'activation_tree', side_effect=AssertionError('SECOND_COPY')):
            with self.assertRaises(module.ReplacementError):
                self.transition()
        self.assertEqual(module.describe(self.home), self.original)

    def test_lost_rollback_before_effect_retains_intent_without_redispatch(self):
        self.transition()
        with patch.object(module, 'exchange', side_effect=module.ReplacementError('NO_RESULT')):
            with self.assertRaises(module.ReplacementError):
                self.transition('rollback')
        self.assertEqual(self.phase(), 'ROLLBACK_INTENT')
        self.assert_start_denied()
        with patch.object(module, 'exchange', side_effect=AssertionError('SECOND_EXCHANGE')):
            for action in ('replace', 'rollback'):
                with self.assertRaisesRegex(module.ReplacementError, 'REPLACEMENT_RECONCILIATION_REQUIRED'):
                    self.transition(action)
        self.assertEqual(module.describe(self.transaction / 'slot'), self.original)

    def test_changed_receipt_prevents_replay_and_rollback(self):
        self.transition()
        receipt = self.transaction / 'receipt.json'
        receipt.write_bytes(receipt.read_bytes() + b' ')
        with patch.object(module, 'exchange', side_effect=AssertionError('UNSAFE_EXCHANGE')):
            for action in ('replace', 'rollback'):
                with self.assertRaisesRegex(module.ReplacementError, 'REPLACEMENT_RECEIPT_CHANGED'):
                    self.transition(action)
        self.assert_start_denied()

    def test_failed_initial_intent_has_no_live_or_private_copy_effect(self):
        with patch.object(self.guard, 'save', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.transition()
        self.assertEqual(self.guard.load()['schema_version'], 4)
        self.assertFalse(self.transaction.exists())
        self.assertEqual(module.describe(self.home), self.original)

    def test_both_locks_and_durable_intent_precede_syscall(self):
        actual = module.exchange
        def locked(*args):
            self.assertEqual(self.phase(), 'REPLACE_INTENT')
            for lock in (journal.maintenance_node_lock(self.lock), self.guard.locked()):
                with self.assertRaises(journal.PanelRequestError):
                    with lock:
                        self.fail('replacement lock missing')
            actual(*args)
        with patch.object(module, 'exchange', side_effect=locked):
            self.transition()

    def test_fsync_failure_after_exchange_reconciles_orientation(self):
        sync = storage.sync_dir
        def failed(path):
            if path == self.transaction and self.home.stat().st_ino != self.original['inode']:
                raise OSError('synthetic')
            return sync(path)
        with patch.object(storage, 'sync_dir', side_effect=failed):
            with self.assertRaises(OSError):
                self.transition()
        self.assertEqual(self.phase(), 'REPLACE_INTENT')
        with patch.object(module, 'exchange', side_effect=AssertionError('SECOND_EXCHANGE')):
            self.assertEqual(self.transition()['files'], 'REPLACED')

    def test_original_tree_drift_rejects_before_mutation(self):
        (self.home / 'x-ui').write_bytes(b'changed after snapshot')
        with self.assertRaisesRegex(module.ReplacementError, 'REPLACEMENT_BASELINE_CHANGED'):
            self.transition()
        self.assertEqual(self.guard.load()['schema_version'], 4)
        self.assertFalse(self.transaction.exists())

    def test_corrupt_retained_tree_or_changed_boot_prevents_rollback(self):
        self.transition()
        with patch.object(self.stop, 'boot', '00000000-0000-4000-8000-000000000099'):
            with self.assertRaisesRegex(module.ReplacementError, 'REPLACEMENT_BOOT_CHANGED'):
                self.transition('rollback')
        (self.transaction / 'slot/x-ui').write_bytes(b'corrupt rollback image')
        with patch.object(module, 'exchange', side_effect=AssertionError('UNSAFE_ROLLBACK')):
            with self.assertRaisesRegex(module.ReplacementError, 'REPLACEMENT_ORIENTATION_UNPROVEN'):
                self.transition('rollback')

    def test_active_service_or_missing_isolation_prevents_exchange(self):
        for change in (patch.object(self.stop, 'active', True), patch.object(PanelIsolation, 'observe', return_value=None)):
            with change, patch.object(module, 'exchange', side_effect=AssertionError('UNSAFE_EXCHANGE')):
                with self.assertRaises((panel_stop.StopError, module.ReplacementError, IsolationError)):
                    self.transition()
            self.assertEqual(self.guard.load()['schema_version'], 4)


if __name__ == '__main__':
    unittest.main()
