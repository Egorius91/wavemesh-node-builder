import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent'))
import panel_maintenance_start as module
import panel_request_guard as journal
import panel_start
import panel_stop
import test_panel_replace as base
from test_panel_candidate import HEAD, OP
from test_panel_stop import BOOT


class MaintenanceContractTest(unittest.TestCase):
    def test_mode_is_required_exactly_once_without_unknown_environment(self):
        self.assertEqual(module.validate_environment([module.MODE]), [module.MODE])
        for entries in ([], ['WAVEMESH_PANEL_MODE='], ['WAVEMESH_PANEL_MODE=normal'],
                        [module.MODE, module.MODE], [module.MODE, 'LD_PRELOAD=evil'],
                        [module.MODE, 'HOME=a', 'HOME=b'], [module.MODE, 'HOME=a\0b'], None, [True]):
            with self.subTest(entries=entries), self.assertRaises(panel_start.StartError):
                module.validate_environment(entries)

    def test_secondary_environment_sources_are_rejected(self):
        controller = module.PanelMaintenanceStart('a' * 64, HEAD)
        properties = {'Environment': {'type': 'as', 'data': [module.MODE]},
                      'EnvironmentFiles': {'type': 'a(sb)', 'data': []},
                      'PassEnvironment': {'type': 'as', 'data': []},
                      'UnsetEnvironment': {'type': 'as', 'data': []}}
        with patch.object(controller, 'property', side_effect=lambda name: properties[name]):
            self.assertEqual(controller.environment(), [module.MODE])
            for name in ('EnvironmentFiles', 'PassEnvironment', 'UnsetEnvironment'):
                old = properties[name]
                properties[name] = {**old, 'data': [['/private/env', True]] if name == 'EnvironmentFiles' else ['WAVEMESH_PANEL_MODE']}
                with self.subTest(name=name), self.assertRaises(panel_start.StartError):
                    controller.environment()
                properties[name] = old

    def test_mode_contract_binds_full_environment_and_approval(self):
        controller = module.PanelMaintenanceStart('a' * 64, HEAD)
        with patch.object(panel_start.PanelStart, 'start_contract', return_value='b' * 64):
            with patch.object(controller, 'environment', return_value=[module.MODE]):
                original = controller.start_contract({}, 'c' * 64, 'd' * 64)
            with patch.object(controller, 'environment', return_value=[module.MODE, 'HOME=/changed']):
                self.assertNotEqual(original, controller.start_contract({}, 'c' * 64, 'd' * 64))
            controller.manifest_sha256 = 'e' * 64
            with patch.object(controller, 'environment', return_value=[module.MODE]):
                self.assertNotEqual(original, controller.start_contract({}, 'c' * 64, 'd' * 64))


@unittest.skipUnless(sys.platform == 'linux' and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'dedicated root Linux CI')
class MaintenanceStateTest(unittest.TestCase):
    transition = base.ReplacementTest.transition

    def setUp(self):
        original = base.fixture
        def fixture():
            manifest, archive, source = original()
            manifest['upstream'] = dict(module.MAINTENANCE_UPSTREAM)
            return manifest, archive, source
        with patch.object(base, 'fixture', side_effect=fixture):
            base.ReplacementTest.setUp(self)
        self.transition()
        self.controller = module.PanelMaintenanceStart(self.manifest, HEAD)
        self.executable = panel_start.file_digest(self.home / 'x-ui')
        patch.object(self.controller, 'observe', side_effect=self.stop.observe).start()
        patch.object(self.controller, 'boot_id', return_value=BOOT).start()
        patch.object(self.controller, 'verify_drained', side_effect=self.stop.verify_drained).start()
        patch.object(self.controller, 'contract', return_value='c' * 64).start()
        patch.object(self.controller, 'job', return_value='/').start()
        patch.object(self.controller, 'start_contract', return_value='e' * 64).start()

    def start(self):
        return self.controller.started(self.guard, OP, 1, self.candidate, self.rollback,
                                       31333, 'c' * 64, self.executable, self.lock)

    def assert_no_dispatch(self):
        before = (self.guard.root / 'state.json').read_bytes()
        with patch.object(self.controller, 'dispatch_start', side_effect=AssertionError('UNSAFE_START')):
            with self.assertRaises((panel_start.StartError, base.module.ReplacementError,
                                    base.candidate.CandidateError, journal.PanelRequestError)):
                with self.start():
                    self.fail('invalid binding admitted')
        self.assertEqual((self.guard.root / 'state.json').read_bytes(), before)

    def test_unknown_start_result_persists_v7_without_second_dispatch_or_release(self):
        with patch.object(self.controller, 'dispatch_start', side_effect=panel_start.StartError('LOST')) as dispatch:
            with self.assertRaisesRegex(panel_start.StartError, 'LOST'):
                with self.start():
                    self.fail('lost result admitted')
            state = self.guard.load()
            self.assertEqual(state['schema_version'], 7)
            self.assertEqual(state['replacement']['phase'], 'REPLACED')
            self.assertEqual(state['start']['phase'], 'START_INTENT')
            with self.assertRaisesRegex(panel_start.StartError, 'START_RECONCILIATION_REQUIRED'):
                with self.start():
                    pass
            self.assertEqual(dispatch.call_count, 1)
        with self.guard.locked():
            with self.assertRaises(journal.PanelRequestError):
                self.guard.maintenance('cancel', OP, 1)
        with self.assertRaises(journal.PanelRequestError):
            self.guard.check_startup(self.lock)
        with self.assertRaisesRegex(panel_start.StartError, 'START_STOP_PROOF_REQUIRED'):
            with panel_start.PanelStart().started(self.guard, OP, 1, self.candidate, self.rollback,
                                                  31333, 'c' * 64, self.executable, self.lock):
                pass
        with self.assertRaisesRegex(panel_stop.StopError, 'STOP_REPLACEMENT_RECONCILIATION_REQUIRED'):
            with self.stop.stopped(self.guard, OP, 1, self.candidate, self.rollback, 31333, 'c' * 64, self.lock):
                pass

    def test_receipt_corruption_prevents_dispatch(self):
        (self.transaction / 'receipt.json').write_bytes(b'{}')
        self.assert_no_dispatch()

    def test_replaced_binary_corruption_prevents_dispatch(self):
        (self.home / 'x-ui').write_bytes(b'changed executable')
        self.assert_no_dispatch()

    def test_wrong_manifest_or_head_prevents_dispatch(self):
        for name, value in (('manifest_sha256', 'f' * 64), ('head', 'f' * 40)):
            with patch.object(self.controller, name, value):
                self.assert_no_dispatch()

    def test_nonterminal_or_rolled_back_replacement_cannot_start(self):
        state = self.guard.load()
        for phase in ('PREPARE_INTENT', 'REPLACE_INTENT', 'ROLLBACK_INTENT', 'ROLLED_BACK'):
            row = {**state['replacement'], 'phase': phase}
            if phase == 'PREPARE_INTENT':
                row['receipt_sha256'] = ''
            with self.guard.locked():
                self.guard.save({**state, 'replacement': row})
            self.assert_no_dispatch()

    def test_v7_only_accepts_replaced_receipt(self):
        state = self.guard.load()
        start = {'phase': 'START_INTENT', 'job_path': '', 'invocation_id': '', 'cgroup_inode': 0,
                 'executable_sha256': self.executable, 'contract_sha256': 'e' * 64}
        with self.guard.locked():
            value = {**state, 'schema_version': 7, 'start': start}
            self.guard.save(value)
            self.assertEqual(self.guard.load(), value)
            self.guard.save({**value, 'replacement': {**state['replacement'], 'phase': 'ROLLED_BACK'}})
            with self.assertRaises(journal.PanelRequestError):
                self.guard.load()


if __name__ == '__main__':
    unittest.main()
