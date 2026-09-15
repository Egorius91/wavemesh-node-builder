import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent'))
import panel_request_guard as journal
import panel_stop as module
from panel_isolation import PanelIsolation, bound_policy

OP = '00000000-0000-4000-8000-000000000001'
BOOT = '00000000-0000-4000-8000-000000000002'


class ContractTest(unittest.TestCase):
    def test_hooks_require_exact_mandatory_guard_and_no_other_effects(self):
        hooks = {key: {'type': 'a(sasbttttuii)', 'data': []} for key in module.HOOKS}
        argv = ['/usr/bin/python3', '-I', '-B', str(module.STARTUP_GUARD), '--check-startup']
        hooks['ExecStartPre']['data'] = [['/usr/bin/python3', argv, False, 0, 0, 0, 0, 0, 0, 0]]
        module.verify_hooks(hooks, module.STARTUP_GUARD)
        for field, value in ((0, '/bin/sh'), (1, ['sh', '-c', 'true']), (2, True), (2, 0)):
            changed = copy.deepcopy(hooks)
            changed['ExecStartPre']['data'][0][field] = value
            with self.assertRaises(module.StopError):
                module.verify_hooks(changed, module.STARTUP_GUARD)
        for key in module.HOOKS:
            changed = copy.deepcopy(hooks)
            changed[key]['data'].append(hooks['ExecStartPre']['data'][0])
            with self.assertRaises(module.StopError):
                module.verify_hooks(changed, module.STARTUP_GUARD)

    def test_stop_binding_is_strict_and_private(self):
        valid = {'phase': 'STOP_INTENT', 'unit': 'x-ui.service', 'boot_id': BOOT,
                 'invocation_id': 'a' * 32, 'control_group': '/system.slice/x-ui.service',
                 'cgroup_inode': 7, 'contract_sha256': 'b' * 64}
        journal.PanelRequestGuard.validate_stop(valid)
        for key in valid:
            for bad in (None, True, [], {}, 'wrong'):
                with self.subTest(key=key, bad=bad), self.assertRaises(journal.PanelRequestError):
                    journal.PanelRequestGuard.validate_stop({**valid, key: bad})
        with self.assertRaises(journal.PanelRequestError):
            journal.PanelRequestGuard.validate_stop({**valid, 'retry': True})


class FakeStop(module.PanelStop):
    def __init__(self, guard):
        self.guard, self.calls, self.active, self.lost = guard, 0, True, False
        self.boot, self.digest, self.invocation = BOOT, 'c' * 64, 'a' * 32

    def observe(self):
        return {**module.SETTINGS, 'User': '', 'NetworkNamespacePath': '',
                'ActiveState': 'active' if self.active else 'inactive',
                'SubState': 'running' if self.active else 'dead',
                'MainPID': '123' if self.active else '0', 'ControlPID': '0',
                'ControlGroup': '/system.slice/x-ui.service', 'InvocationID': self.invocation}

    def boot_id(self):
        return self.boot

    def contract(self, observed, helper):
        return self.digest

    def cgroup(self):
        return 7, self.active

    def dispatch_stop(self):
        assert self.guard.load()['schema_version'] == 4
        self.calls += 1
        self.active = False
        if self.lost:
            raise module.StopError('SYNTHETIC_LOST_RESULT')


@unittest.skipUnless(sys.platform == 'linux' and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'dedicated root Linux CI')
class StopTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wm-stop-unit-', dir='/run')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.guard = journal.PanelRequestGuard(self.root / 'journal')
        self.lock = self.root / 'node.lock'
        with journal.maintenance_node_lock(self.lock), self.guard.locked():
            self.guard.maintenance('prepare', OP, 1)
        with self.guard.installation_intent(OP, 1, 'a'*64, 'b'*64, self.lock):
            pass
        default = patch.object(journal, 'DEFAULT_ROOT', self.guard.root)
        default.start(); self.addCleanup(default.stop)
        policy = bound_policy(OP, 1, 'a'*64, 'b'*64, 31333)
        isolated = patch.object(PanelIsolation, 'observe', return_value={'nftables': policy})
        isolated.start(); self.addCleanup(isolated.stop)
        self.stop = FakeStop(self.guard)

    def context(self):
        return self.stop.stopped(self.guard, OP, 1, 'a'*64, 'b'*64, 31333, 'd'*64, self.lock)

    def test_durable_before_stop_and_locks_hold_through_body(self):
        with self.context() as result:
            self.assertEqual(result['backend_cgroup'], 'DRAINED')
            with self.assertRaises(journal.PanelRequestError):
                with journal.maintenance_node_lock(self.lock):
                    pass
            with self.assertRaises(journal.PanelRequestError):
                with self.guard.locked():
                    pass
        self.assertEqual(self.stop.calls, 1)
        with self.guard.locked():
            self.assertEqual(self.guard.maintenance('status')['stop'], {'phase': 'STOP_INTENT'})
            with self.assertRaises(journal.PanelRequestError):
                self.guard.maintenance('cancel', OP, 1)
        with self.assertRaises(journal.PanelRequestError):
            self.guard.check_startup(self.lock)

    def test_committed_lost_result_reconciles_without_second_stop(self):
        self.stop.lost = True
        with self.assertRaises(module.StopError):
            with self.context():
                pass
        before = (self.guard.root / 'state.json').read_bytes()
        with self.context() as result:
            self.assertTrue(result['reconciliation_required'])
        self.assertEqual(self.stop.calls, 1)
        self.assertEqual(before, (self.guard.root / 'state.json').read_bytes())

    def test_lost_before_commit_does_not_retry_active_service(self):
        with patch.object(self.stop, 'dispatch_stop', side_effect=module.StopError('LOST')):
            with self.assertRaises(module.StopError):
                with self.context():
                    pass
        with self.assertRaises(module.StopError):
            with self.context():
                pass
        self.assertEqual(self.stop.calls, 0)

    def test_new_boot_invocation_or_contract_reject(self):
        with self.context():
            pass
        for key, bad in (('boot', '00000000-0000-4000-8000-000000000003'),
                         ('digest', 'e'*64), ('invocation', 'f'*32)):
            with patch.object(self.stop, key, bad), self.assertRaises(module.StopError):
                with self.context():
                    pass
        self.assertEqual(self.stop.calls, 1)

    def test_failed_journal_write_never_stops(self):
        with patch.object(self.guard, 'save', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                with self.context():
                    pass
        self.assertEqual(self.stop.calls, 0)

    def test_populated_or_replaced_cgroup_never_yields(self):
        with self.context():
            pass
        for result in ((7, True), (8, False)):
            with patch.object(self.stop, 'cgroup', return_value=result), self.assertRaises(module.StopError):
                with self.context():
                    pass
        self.assertEqual(self.stop.calls, 1)


if __name__ == '__main__':
    unittest.main()
