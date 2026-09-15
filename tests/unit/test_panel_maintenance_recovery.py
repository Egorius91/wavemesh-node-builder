import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent'))
import panel_maintenance_recovery as module
import panel_request_guard as journal
import panel_start
import panel_stop
from test_panel_candidate import HEAD, OP
from test_panel_stop import BOOT
import test_panel_maintenance_start as base

ATTEMPT = '00000000-0000-4000-8000-000000000010'
NEXT_ATTEMPT = '00000000-0000-4000-8000-000000000011'
PRIOR = 'd' * 32
START = {'phase': 'START_ADMITTED', 'job_path': '/org/freedesktop/systemd1/job/42',
         'invocation_id': PRIOR, 'cgroup_inode': 8, 'executable_sha256': 'b' * 64, 'contract_sha256': 'e' * 64}


class RecoverySchemaTest(unittest.TestCase):
    def test_strict_history_and_phase_constraints(self):
        record = {'attempt_id': ATTEMPT, 'previous_start': START, 'terminal_state': 'inactive', 'terminal_cgroup_inode': 0}
        current = {**START, 'invocation_id': 'f' * 32}
        journal.PanelRequestGuard.validate_recoveries([record], current)
        for bad in ([], [record] * 2, [record] * 17, [{**record, 'extra': True}],
                    [{**record, 'terminal_state': 'active'}], [{**record, 'terminal_cgroup_inode': True}],
                    [{**record, 'terminal_cgroup_inode': 9}], [{**record, 'attempt_id': 'invalid'}],
                    [{**record, 'previous_start': {**START, 'phase': 'START_INTENT'}}]):
            with self.subTest(bad=bad), self.assertRaises(journal.PanelRequestError):
                journal.PanelRequestGuard.validate_recoveries(bad, current)
        with self.assertRaises(journal.PanelRequestError):
            journal.PanelRequestGuard.validate_recoveries([record], START)


@unittest.skipUnless(sys.platform == 'linux' and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'dedicated root Linux CI')
class RecoveryStateTest(unittest.TestCase):
    transition = base.MaintenanceStateTest.transition

    def setUp(self):
        base.MaintenanceStateTest.setUp(self)
        self.controller = module.PanelMaintenanceRecovery(self.manifest, HEAD)
        state = self.guard.load()
        self.start_state = {**START, 'executable_sha256': self.executable}
        with self.guard.locked():
            self.guard.save({**state, 'schema_version': 7, 'start': self.start_state})
        self.observed = {**self.stop.observe(), 'ActiveState': 'inactive', 'SubState': 'dead',
                         'MainPID': '0', 'ControlPID': '0', 'InvocationID': PRIOR, 'ControlGroup': ''}
        patch.object(self.controller, 'observe', side_effect=lambda: dict(self.observed)).start()
        patch.object(self.controller, 'boot_id', return_value=BOOT).start()
        patch.object(self.controller, 'cgroup', return_value=(0, False)).start()
        patch.object(self.controller, 'job', return_value='/').start()
        patch.object(self.controller, 'start_contract', return_value='e' * 64).start()

    def recover(self, attempt=ATTEMPT, previous=PRIOR):
        return self.controller.recovered(self.guard, OP, 1, self.candidate, self.rollback,
            31333, 'c' * 64, self.executable, attempt, previous, self.lock)

    def test_terminal_inactive_and_failed_are_observed_without_commands(self):
        for active, sub in (('inactive', 'dead'), ('failed', 'failed')):
            self.observed.update(ActiveState=active, SubState=sub)
            self.assertEqual(self.controller.terminal(self.guard.load(), 'c' * 64, self.executable), (active, 0))

    def test_active_pending_wrong_invocation_and_descendants_prevent_dispatch(self):
        baseline = dict(self.observed)
        for changes in ({'ActiveState': 'active', 'SubState': 'running'}, {'ControlPID': '1'},
                        {'MainPID': '1'}, {'InvocationID': 'f' * 32}):
            self.observed = {**baseline, **changes}
            with patch.object(self.controller, 'dispatch_start', side_effect=AssertionError('UNSAFE_START')):
                with self.assertRaises(panel_start.StartError):
                    with self.recover():
                        pass
        self.observed = baseline
        for name, value in (('job', '/org/freedesktop/systemd1/job/50'), ('cgroup', (8, True)), ('cgroup', (9, False)), ('boot_id', 'different')):
            with patch.object(self.controller, name, return_value=value), self.assertRaises(panel_start.StartError):
                with self.recover():
                    pass
        self.assertEqual(self.guard.load()['schema_version'], 7)

    def test_unknown_dispatch_is_durable_and_not_retried_with_old_or_new_id(self):
        def lost():
            state = self.guard.load()
            self.assertEqual(state['schema_version'], 8)
            self.assertEqual(state['recoveries'][0]['previous_start'], self.start_state)
            self.assertEqual(state['start']['phase'], 'START_INTENT')
            raise panel_start.StartError('LOST')
        with patch.object(self.controller, 'dispatch_start', side_effect=lost) as dispatch:
            with self.assertRaisesRegex(panel_start.StartError, 'LOST'):
                with self.recover():
                    pass
            before = (self.guard.root / 'state.json').read_bytes()
            for attempt in (ATTEMPT, NEXT_ATTEMPT):
                with self.assertRaises(panel_start.StartError):
                    with self.recover(attempt):
                        pass
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual((self.guard.root / 'state.json').read_bytes(), before)
        self.assert_closed()

    def assert_closed(self):
        with self.guard.locked(), self.assertRaises(journal.PanelRequestError):
            self.guard.maintenance('cancel', OP, 1)
        with self.assertRaises(journal.PanelRequestError):
            self.guard.check_startup(self.lock)
        with self.assertRaises(journal.PanelRequestError):
            self.guard.execute('POST', '/panel/api/clients/add', 'fixture', {}, lambda: self.fail('writer bypass'))
        with self.assertRaises(panel_start.StartError):
            with panel_start.PanelStart().started(self.guard, OP, 1, self.candidate, self.rollback, 31333, 'c' * 64, self.executable, self.lock):
                pass
        with self.assertRaises(panel_stop.StopError):
            with self.stop.stopped(self.guard, OP, 1, self.candidate, self.rollback, 31333, 'c' * 64, self.lock):
                pass

    def test_same_id_reconciles_without_dispatch_and_stale_id_is_rejected(self):
        state = self.guard.load()
        row = {'attempt_id': ATTEMPT, 'previous_start': self.start_state, 'terminal_state': 'inactive', 'terminal_cgroup_inode': 0}
        current = {**self.start_state, 'invocation_id': 'f' * 32, 'job_path': '/org/freedesktop/systemd1/job/43'}
        with self.guard.locked():
            self.guard.save({**state, 'schema_version': 8, 'recoveries': [row], 'start': current})
        before = (self.guard.root / 'state.json').read_bytes()
        with patch.object(self.controller, 'verify_running') as verify, patch.object(self.controller, 'dispatch_start', side_effect=AssertionError('SECOND_START')):
            with self.recover() as receipt:
                self.assertTrue(receipt['reconciliation_required'])
            verify.assert_called_once()
            with self.assertRaises(panel_start.StartError):
                with self.recover(previous='a' * 32):
                    pass
        self.assertEqual((self.guard.root / 'state.json').read_bytes(), before)
        self.assert_closed()

    def test_controller_death_retains_intent(self):
        import multiprocessing
        def die():
            with patch.object(self.controller, 'dispatch_start', side_effect=lambda: os._exit(86)):
                with self.recover():
                    os._exit(87)
        child = multiprocessing.get_context('fork').Process(target=die)
        child.start(); child.join(timeout=5)
        if child.is_alive():
            child.kill(); child.join(timeout=5)
            self.fail('crash fixture stuck')
        self.assertEqual(child.exitcode, 86)
        self.assertEqual(self.guard.load()['start']['phase'], 'START_INTENT')
        self.assertTrue((self.guard.root / 'start.sock').exists())
        with self.assertRaises(panel_start.StartError):
            with self.recover():
                pass
        self.assert_closed()


if __name__ == '__main__':
    unittest.main()
