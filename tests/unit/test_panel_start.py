import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent'))
import panel_request_guard as journal
import panel_start as module
import panel_stop
from panel_isolation import PanelIsolation, bound_policy
from test_panel_stop import FakeStop, OP, BOOT

INTENT = {'phase': 'START_INTENT', 'job_path': '', 'invocation_id': '', 'cgroup_inode': 0,
          'executable_sha256': 'd'*64, 'contract_sha256': 'e'*64}
ADMITTED = {**INTENT, 'phase': 'START_ADMITTED', 'job_path': '/org/freedesktop/systemd1/job/42',
            'invocation_id': 'f'*32, 'cgroup_inode': 8}


class StartContractTest(unittest.TestCase):
    def test_exact_start_schema_and_phase_constraints(self):
        for value in (INTENT, ADMITTED):
            journal.PanelRequestGuard.validate_start(value)
            for key in value:
                for bad in (None, [], {}, True):
                    with self.subTest(key=key), self.assertRaises(journal.PanelRequestError):
                        journal.PanelRequestGuard.validate_start({**value, key: bad})
            with self.assertRaises(journal.PanelRequestError):
                journal.PanelRequestGuard.validate_start({**value, 'permit': True})
        for bad in ({**INTENT, 'invocation_id': 'f'*32}, {**ADMITTED, 'job_path': ''},
                    {**ADMITTED, 'cgroup_inode': 0}, {**INTENT, 'job_path': '/other'}):
            with self.assertRaises(journal.PanelRequestError):
                journal.PanelRequestGuard.validate_start(bad)

    def test_dispatch_requires_single_exact_job_object(self):
        import json
        controller = module.PanelStart()
        for value in ({'type': 'o', 'data': []}, {'type': 'o', 'data': ['/wrong']},
                      {'type': 's', 'data': ['/org/freedesktop/systemd1/job/42']}):
            with patch.object(controller, 'run', return_value=json.dumps(value).encode()):
                with self.assertRaises(module.StartError):
                    controller.dispatch_start()
        with patch.object(controller, 'run', return_value=b'{"type":"o","data":["/org/freedesktop/systemd1/job/42"]}') as run:
            self.assertEqual(controller.dispatch_start(), ADMITTED['job_path'])
            self.assertEqual(run.call_args.args[0][-4:], ['StartUnit', 'ss', 'x-ui.service', 'fail'])


@unittest.skipUnless(sys.platform == 'linux' and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'dedicated root Linux CI')
class StartStateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wm-start-', dir='/run')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.guard = journal.PanelRequestGuard(self.root / 'journal')
        self.lock = self.root / 'node.lock'
        self.addCleanup(patch.stopall)
        patch.object(journal, 'DEFAULT_ROOT', self.guard.root).start()
        self.expected = bound_policy(OP, 1, 'a'*64, 'b'*64, 31333)
        patch.object(PanelIsolation, 'observe', return_value={'nftables': self.expected}).start()
        with journal.maintenance_node_lock(self.lock), self.guard.locked():
            self.guard.maintenance('prepare', OP, 1)
        with FakeStop(self.guard).stopped(self.guard, OP, 1, 'a'*64, 'b'*64, 31333, 'c'*64, self.lock):
            pass
        self.controller = module.PanelStart()
        observed = FakeStop(None); observed.active = False
        patch.object(self.controller, 'observe', side_effect=observed.observe).start()
        patch.object(self.controller, 'boot_id', return_value=BOOT).start()
        patch.object(self.controller, 'verify_drained').start()
        patch.object(self.controller, 'contract', return_value='c'*64).start()
        patch.object(self.controller, 'job', return_value='/').start()
        patch.object(self.controller, 'start_contract', return_value='e'*64).start()

    def start(self):
        return self.controller.started(self.guard, OP, 1, 'a'*64, 'b'*64, 31333, 'c'*64, 'd'*64, self.lock)

    def persist_start(self, value):
        with self.guard.locked():
            state = self.guard.load()
            self.guard.save({**state, 'schema_version': 5, 'start': value})

    def test_uncertain_dispatch_is_durable_and_never_retried(self):
        def lose():
            self.assertEqual(self.guard.load()['start'], INTENT)
            raise module.StartError('LOST_DISPATCH')
        with patch.object(self.controller, 'dispatch_start', side_effect=lose) as dispatch:
            with self.assertRaises(module.StartError):
                with self.start():
                    self.fail('unknown dispatch yielded')
            before = (self.guard.root / 'state.json').read_bytes()
            with self.assertRaises(module.StartError):
                with self.start():
                    pass
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual((self.guard.root / 'state.json').read_bytes(), before)

    def test_controller_process_death_retains_intent_and_stale_socket(self):
        import multiprocessing
        def crash():
            with patch.object(self.controller, 'dispatch_start', side_effect=lambda: os._exit(86)):
                with self.start():
                    os._exit(87)
        child = multiprocessing.get_context('fork').Process(target=crash)
        child.start()
        child.join(timeout=5)
        if child.is_alive():
            child.kill(); child.join(timeout=5)
            self.fail('crash fixture did not terminate')
        self.assertEqual(child.exitcode, 86)
        self.assertEqual(self.guard.load()['start'], INTENT)
        endpoint = self.guard.root / 'start.sock'
        self.assertTrue(endpoint.exists())
        before = (self.guard.root / 'state.json').read_bytes()
        with patch.object(self.controller, 'dispatch_start') as dispatch:
            with self.assertRaises(module.StartError):
                with self.start():
                    pass
            dispatch.assert_not_called()
        self.assertTrue(endpoint.exists())
        self.assertEqual((self.guard.root / 'state.json').read_bytes(), before)
        with self.assertRaises(journal.PanelRequestError):
            self.guard.check_startup(self.lock)

    def test_failed_intent_save_never_dispatches(self):
        with patch.object(self.guard, 'save', side_effect=OSError('synthetic')), patch.object(self.controller, 'dispatch_start') as dispatch:
            with self.assertRaises(OSError):
                with self.start():
                    pass
            dispatch.assert_not_called()

    def test_admitted_replay_verifies_only_and_keeps_both_locks(self):
        self.persist_start(ADMITTED)
        before = (self.guard.root / 'state.json').read_bytes()
        with patch.object(self.controller, 'verify_running') as verify, patch.object(self.controller, 'dispatch_start') as dispatch:
            with self.start() as result:
                self.assertTrue(result['reconciliation_required'])
                with self.assertRaises(journal.PanelRequestError):
                    with journal.maintenance_node_lock(self.lock):
                        pass
                with self.assertRaises(journal.PanelRequestError):
                    with self.guard.locked():
                        pass
            verify.assert_called_once()
            dispatch.assert_not_called()
        self.assertEqual((self.guard.root / 'state.json').read_bytes(), before)

    def test_v5_cannot_be_cancelled_restarted_via_stop_or_used_for_writes(self):
        self.persist_start(ADMITTED)
        with self.guard.locked():
            status = self.guard.maintenance('status')
            self.assertEqual(status['start'], {'phase': 'START_ADMITTED'})
            self.assertNotIn(ADMITTED['invocation_id'], str(status))
            with self.assertRaises(journal.PanelRequestError):
                self.guard.maintenance('cancel', OP, 1)
        with self.assertRaises(journal.PanelRequestError):
            self.guard.check_startup(self.lock)
        stop = FakeStop(self.guard)
        with self.assertRaisesRegex(panel_stop.StopError, 'STOP_START_RECONCILIATION_REQUIRED'):
            with stop.stopped(self.guard, OP, 1, 'a'*64, 'b'*64, 31333, 'c'*64, self.lock):
                pass
        self.assertEqual(stop.calls, 0)
        with self.assertRaises(journal.PanelRequestError):
            self.guard.execute('POST', '/panel/api/clients/add', 'test', {}, lambda: self.fail('write'))

    def test_stale_socket_is_not_deleted_or_reused(self):
        endpoint = self.guard.root / 'start.sock'
        endpoint.write_text('owned-by-something-else')
        with patch.object(self.controller, 'dispatch_start') as dispatch:
            with self.assertRaises(OSError):
                with self.start():
                    pass
            dispatch.assert_not_called()
        self.assertEqual(endpoint.read_text(), 'owned-by-something-else')

    def test_wrong_peer_never_receives_grant_or_changes_state(self):
        # Real SO_PEERCRED from an ordinary root process is not the unit's
        # ExecStartPre ControlPID, even with the correct protocol bytes.
        self.persist_start({**INTENT, 'job_path': ADMITTED['job_path']})
        before = (self.guard.root / 'state.json').read_bytes()
        client, server = socket.socketpair()
        with client, server, self.guard.locked():
            client.sendall(b'WAVEMESH_START_V1\n')
            with self.assertRaisesRegex(module.StartError, 'START_PEER_REJECTED'):
                self.controller.admit(self.guard, server, self.guard.load(), 'c'*64, 'd'*64, self.expected)
        self.assertEqual((self.guard.root / 'state.json').read_bytes(), before)

    def test_failed_admission_save_never_sends_ok(self):
        self.persist_start({**INTENT, 'job_path': ADMITTED['job_path']})
        client, server = socket.socketpair()
        with client, server, self.guard.locked():
            client.sendall(b'WAVEMESH_START_V1\n')
            with patch.object(self.controller, 'peer_identity', return_value=('f'*32, 8)), patch.object(self.guard, 'save', side_effect=OSError('synthetic')):
                with self.assertRaises(OSError):
                    self.controller.admit(self.guard, server, self.guard.load(), 'c'*64, 'd'*64, self.expected)
            client.setblocking(False)
            with self.assertRaises(BlockingIOError):
                client.recv(3)

    def test_lost_grant_is_consumed_and_not_granted_again(self):
        self.persist_start({**INTENT, 'job_path': ADMITTED['job_path']})
        client, server = socket.socketpair()
        with client, server, self.guard.locked():
            client.sendall(b'WAVEMESH_START_V1\n')
            client.shutdown(socket.SHUT_RD)
            with patch.object(self.controller, 'peer_identity', return_value=('f'*32, 8)):
                with self.assertRaises(BrokenPipeError):
                    self.controller.admit(self.guard, server, self.guard.load(), 'c'*64, 'd'*64, self.expected)
            self.assertEqual(self.guard.load()['start'], ADMITTED)
            with self.assertRaisesRegex(module.StartError, 'START_RECONCILIATION_REQUIRED'):
                self.controller.admit(self.guard, server, self.guard.load(), 'c'*64, 'd'*64, self.expected)


if __name__ == '__main__':
    unittest.main()
