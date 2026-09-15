#!/usr/bin/env python3
"""Recovery activation through real systemd ExecStartPre and peer credentials."""
import hashlib
import os
from pathlib import Path
import socket
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent'))
import panel_request_guard as journal
import panel_start
import panel_stop
import test_panel_stop_systemd as fixture


def recovery_checks(root, unit, guard, lock):
    # The controller's production executable/argv are fixed. Only this disposable
    # fixture uses Python and its synthetic worker; no production path is changed.
    panel_start.PANEL_BINARY = Path('/usr/bin/python3').resolve()
    panel_start.PANEL_ARGV = [str(panel_start.PANEL_BINARY), str(root / 'worker.py')]
    helper = hashlib.sha256(panel_stop.STARTUP_GUARD.read_bytes()).hexdigest()
    executable = panel_start.file_digest(panel_start.PANEL_BINARY)
    controller = panel_start.PanelStart()
    def start():
        return controller.started(guard, fixture.OP, 1, 'a'*64, 'b'*64,
                                  fixture.PORT, helper, executable, lock)
    verify = controller.verify_running
    def lose_result(*args):
        verify(*args)
        raise panel_start.StartError('SYNTHETIC_LOST_START_RESULT')
    with patch.object(controller, 'verify_running', side_effect=lose_result):
        try:
            with start():
                raise AssertionError('LOST_START_NOT_REPORTED')
        except panel_start.StartError as exc:
            assert str(exc) == 'SYNTHETIC_LOST_START_RESULT', 'UNEXPECTED_START_ERROR'
    before = (guard.root / 'state.json').read_bytes()
    state = guard.load()
    assert state['schema_version'] == 5 and state['start']['phase'] == 'START_ADMITTED'
    assert state['start']['invocation_id'] != state['stop']['invocation_id']
    assert not (guard.root / 'start.sock').exists()
    with patch.object(controller, 'dispatch_start', side_effect=AssertionError('SECOND_START')):
        with patch.object(controller, 'admit', side_effect=AssertionError('SECOND_GRANT')):
            with start() as receipt:
                assert receipt['reconciliation_required']
                assert receipt['local_admission'] == 'CLOSED'
                for check in (lambda: guard.check_startup(lock),
                              lambda: guard.execute('POST', '/panel/api/clients/add', 'fixture', {},
                                                    lambda: (_ for _ in ()).throw(AssertionError('WRITER_BYPASS')))):
                    try:
                        check()
                        raise AssertionError('ORDINARY_ADMISSION_BYPASS')
                    except journal.PanelRequestError:
                        pass
                deadline = time.monotonic() + 5
                while True:
                    try:
                        connection = socket.create_connection(('127.0.0.1', fixture.PORT), timeout=1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise AssertionError('RECOVERED_CHILD_NOT_READY')
                        time.sleep(0.05)
                with connection:
                    connection.sendall(b'probe')
                    assert connection.recv(5) == b'probe', 'RECOVERED_CONNECTION_FAILED'
    assert (guard.root / 'state.json').read_bytes() == before
    print('REAL_SYSTEMD_JOB_PEER_ADMISSION_WITH_LOCKS_RETAINED=PASS', flush=True)
    print('LOST_START_RESULT_RECONCILED_WITHOUT_START_OR_GRANT_REPLAY=PASS', flush=True)
    print('RECOVERED_CHILD_HEALTHY_WITH_ORDINARY_WRITERS_CLOSED=PASS', flush=True)


if __name__ == '__main__':
    try:
        os.environ['WAVEMESH_CI_RECOVERY_TEST'] = 'true'
        fixture.main()
    except Exception as exc:
        import re
        print('PANEL_RECOVERY_START=FAILED; TYPE=' + type(exc).__name__, file=sys.stderr)
        if isinstance(exc, (panel_start.StartError, panel_stop.StopError, AssertionError, RuntimeError)) and re.fullmatch('[A-Z_]+', str(exc)):
            print('CODE=' + str(exc), file=sys.stderr)
        raise SystemExit(1)
