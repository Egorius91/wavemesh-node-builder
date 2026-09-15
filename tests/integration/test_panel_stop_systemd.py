#!/usr/bin/env python3
"""Combined real nft + installed startup guard + systemd descendant stop in CI."""
import hashlib
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent'))
import panel_request_guard as journal
import panel_stop as module
from panel_isolation import PanelIsolation

OP = '00000000-0000-4000-8000-000000000001'
PORT = 31333


def run(args, required=True):
    result = subprocess.run(args, capture_output=True, timeout=30)
    if required and result.returncode:
        raise RuntimeError('FIXTURE_COMMAND_FAILED')
    return result


def inner(root, unit):
    journal.DEFAULT_ROOT = root / 'journal'
    journal.PANEL_UNIT = unit
    module.STARTUP_GUARD = root / 'package/usr/local/lib/wavemesh/lib/panel_request_guard.py'
    digest = hashlib.sha256(module.STARTUP_GUARD.read_bytes()).hexdigest()
    guard = journal.PanelRequestGuard(journal.DEFAULT_ROOT)
    lock = root / 'node.lock'
    stopper = module.PanelStop()
    run(['systemctl', 'start', unit])
    deadline = time.monotonic() + 10
    while not (root / 'child-ready').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('CHILD_NOT_READY')
        time.sleep(0.05)
    observed = stopper.observe()
    parent = os.pidfd_open(int(observed['MainPID']))
    child = os.pidfd_open(int((root / 'child-ready').read_text()))
    connection = socket.create_connection(('127.0.0.1', PORT), timeout=2)
    try:
        connection.sendall(b'probe')
        assert connection.recv(5) == b'probe', 'BASELINE_CONNECTION_FAILED'
        with journal.maintenance_node_lock(lock), guard.locked():
            guard.maintenance('prepare', OP, 1)
        PanelIsolation().isolate(guard, OP, 1, 'a'*64, 'b'*64, PORT, lock)
        connection.sendall(b'probe')
        assert connection.recv(5) == b'probe', 'ROOT_CONTROL_FAILED'
        print('ACTIVE_SERVICE_AND_ROOT_CONNECTION_WITH_ISOLATION=PASS', flush=True)

        def stopped():
            return stopper.stopped(guard, OP, 1, 'a'*64, 'b'*64, PORT, digest, lock)

        extra = Path('/run/systemd/system') / (unit + '.d') / '99-extra.conf'
        # These declarations are reloaded, never started/mounted. A controller
        # must reject them even when the currently running service is healthy.
        for section, directive in (
            ('Service', 'PrivateNetwork=yes'),
            ('Unit', 'JoinsNamespaceOf=wm-absent-ci-peer.service'),
            ('Service', 'MountImages=/nonexistent-wm-ci.raw:/opt/wm-ci'),
            ('Service', 'ExtensionImages=/nonexistent-wm-ci.raw'),
            ('Service', 'ExtensionDirectories=/nonexistent-wm-ci'),
        ):
            before = (guard.root / 'state.json').read_bytes()
            extra.write_text('[' + section + ']\n' + directive + '\n')
            run(['systemctl', 'daemon-reload'])
            try:
                with patch.object(stopper, 'dispatch_stop', side_effect=AssertionError('UNSAFE_STOP_DISPATCH')):
                    try:
                        with stopped():
                            raise AssertionError('UNSUPPORTED_ENVIRONMENT_ACCEPTED')
                    except module.StopError as exc:
                        assert str(exc) == 'STOP_EXECUTION_ENVIRONMENT_UNSUPPORTED', 'UNEXPECTED_ENVIRONMENT_ERROR'
            finally:
                extra.unlink()
                run(['systemctl', 'daemon-reload'])
            assert (guard.root / 'state.json').read_bytes() == before
            assert stopper.observe()['ActiveState'] == 'active'
        print('UNSUPPORTED_NAMESPACES_AND_IMAGES_REJECTED_BEFORE_STOP=PASS', flush=True)

        # After daemon-reload the declared path may differ from the running
        # process. From the host namespace, the old path-only check would pass;
        # verify the actual process catches this mismatch. This subprocess only
        # reads the fixture contract; it never applies host firewall rules.
        extra.write_text('[Service]\nNetworkNamespacePath=/proc/1/ns/net\n')
        run(['systemctl', 'daemon-reload'])
        try:
            run(['nsenter', '-t', '1', '-n', sys.executable, str(Path(__file__).resolve()),
                 'namespace-mismatch', str(root), unit])
        finally:
            extra.unlink()
            run(['systemctl', 'daemon-reload'])
        print('ACTUAL_PROCESS_NAMESPACE_MISMATCH_REJECTED=PASS', flush=True)

        # Reject a stop-post hook before dispatch; only fixture configuration.
        extra.write_text('[Service]\nExecStopPost=/usr/bin/true\n')
        run(['systemctl', 'daemon-reload'])
        try:
            with stopped():
                raise AssertionError('UNSAFE_HOOK_ACCEPTED')
        except module.StopError as exc:
            assert str(exc) == 'STOP_HOOK_INVALID', 'UNEXPECTED_HOOK_ERROR'
        finally:
            extra.unlink()
            run(['systemctl', 'daemon-reload'])
        assert stopper.observe()['ActiveState'] == 'active'
        original = stopper.dispatch_stop
        def lose_result():
            assert guard.load()['schema_version'] == 4, 'INTENT_NOT_DURABLE'
            original()
            raise module.StopError('SYNTHETIC_LOST_RESULT')
        with patch.object(stopper, 'dispatch_stop', side_effect=lose_result):
            try:
                with stopped():
                    raise AssertionError('LOST_RESULT_NOT_REPORTED')
            except module.StopError as exc:
                assert str(exc) == 'SYNTHETIC_LOST_RESULT', 'UNEXPECTED_STOP_ERROR'
        before = (guard.root / 'state.json').read_bytes()
        try:
            with patch.object(stopper, 'dispatch_stop', side_effect=AssertionError('SECOND_STOP')):
                with stopped() as receipt:
                    assert receipt['backend_cgroup'] == 'DRAINED'
                    assert receipt['reconciliation_required']
                    for fd in (parent, child):
                        poll = select.poll(); poll.register(fd, select.POLLIN)
                        assert poll.poll(0), 'ORIGINAL_PROCESS_STILL_LIVE'
                    try:
                        connection.sendall(b'probe')
                        assert connection.recv(5) == b'', 'ESTABLISHED_CONNECTION_SURVIVED'
                    except (ConnectionResetError, BrokenPipeError):
                        pass
                    print('ORIGINAL_PARENT_CHILD_AND_ESTABLISHED_CONNECTION_DRAINED=PASS', flush=True)
        except module.StopError:
            state = stopper.observe()
            # Only fixed state words and booleans from our disposable CI unit.
            # No invocation, cgroup, endpoint or process identifiers are logged.
            for name in ('ActiveState', 'SubState'):
                value = state[name]
                print('CI_' + name.upper() + '=' + (value if re.fullmatch('[a-z-]+', value) else 'OTHER'), flush=True)
            print('CI_MAIN_ZERO=' + str(state['MainPID'] == '0'), flush=True)
            print('CI_CONTROL_ZERO=' + str(state['ControlPID'] == '0'), flush=True)
            print('CI_INVOCATION_EMPTY_OR_ORIGINAL=' + str(state['InvocationID'] in ('', observed['InvocationID'])), flush=True)
            raise
        assert (guard.root / 'state.json').read_bytes() == before
        if os.environ.get('WAVEMESH_CI_RECOVERY_TEST') == 'true':
            from test_panel_start_systemd import recovery_checks
            recovery_checks(root, unit, guard, lock)
            # Fixture cleanup stop, not the production recovery controller.
            run(['systemctl', 'stop', unit])
            before = (guard.root / 'state.json').read_bytes()
        assert run(['systemctl', 'start', unit], required=False).returncode != 0, 'STARTUP_BYPASSED'
        assert (guard.root / 'state.json').read_bytes() == before
        print('LOST_STOP_RESULT_RECONCILED_WITHOUT_REDISPATCH=PASS', flush=True)
        print('RETAINED_STOP_INTENT_BLOCKS_SYSTEMD_STARTUP=PASS', flush=True)
    finally:
        connection.close()
        os.close(parent); os.close(child)


def main():
    if sys.platform != 'linux' or os.geteuid() != 0 or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('DEDICATED_GITHUB_ROOT_REQUIRED')
    if sys.argv[1:2] == ['inner']:
        inner(Path(sys.argv[2]), sys.argv[3])
        return
    if sys.argv[1:2] == ['namespace-mismatch']:
        journal.PANEL_UNIT = sys.argv[3]
        module.STARTUP_GUARD = Path(sys.argv[2]) / 'package/usr/local/lib/wavemesh/lib/panel_request_guard.py'
        digest = hashlib.sha256(module.STARTUP_GUARD.read_bytes()).hexdigest()
        stopper = module.PanelStop()
        try:
            stopper.contract(stopper.observe(), digest)
        except module.StopError as exc:
            assert str(exc) == 'STOP_NETWORK_NAMESPACE_MISMATCH', 'WRONG_NAMESPACE_ERROR'
            return
        raise AssertionError('PROCESS_NAMESPACE_MISMATCH_ACCEPTED')
    if Path('/proc/1/comm').read_text().strip() != 'systemd':
        raise RuntimeError('SYSTEMD_REQUIRED')
    with tempfile.TemporaryDirectory(prefix='wm-stop-ci-', dir='/run') as directory:
        root = Path(directory)
        unit_name = root.name + '.service'
        unit = Path('/run/systemd/system') / unit_name
        drop_dir = Path(str(unit) + '.d')
        drop_file = drop_dir / '50-wavemesh-panel-startup.conf'
        if unit.exists() or unit.is_symlink() or drop_dir.exists() or drop_dir.is_symlink():
            raise RuntimeError('FIXTURE_COLLISION')
        keeper = subprocess.Popen(['unshare', '--net', sys.executable, '-c',
                                   'import sys;print("READY",flush=True);sys.stdin.read()'],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([keeper.stdout], [], [], 5)
            assert ready and keeper.stdout.readline().strip() == 'READY', 'NAMESPACE_NOT_READY'
            run(['nsenter', '-t', str(keeper.pid), '-n', 'ip', 'link', 'set', 'lo', 'up'])
            package = root / 'package'
            run(['bash', '-c', 'set -Eeuo pipefail; source "$1"; wm_install_cli "$2"',
                 'fixture', str(ROOT / 'scripts/00_common.sh'), str(package)])
            installed = package / 'usr/local/lib/wavemesh/lib/panel_request_guard.py'
            assert installed.read_bytes() == (ROOT / 'agent/panel_request_guard.py').read_bytes()
            source = installed.read_text().replace('/var/lib/wavemesh-agent/panel-requests', str(root / 'journal'))
            source = source.replace('/run/lock/wavemesh-node.lock', str(root / 'node.lock'))
            source = source.replace('PANEL_UNIT = "x-ui.service"', 'PANEL_UNIT = ' + repr(unit_name))
            installed.write_text(source)
            template = package / 'usr/local/lib/wavemesh/systemd/50-wavemesh-panel-startup.conf'
            assert template.read_bytes() == (ROOT / 'systemd/50-wavemesh-panel-startup.conf').read_bytes()
            drop = template.read_text().replace('/usr/local/lib/wavemesh/lib/panel_request_guard.py', str(installed))
            guard = journal.PanelRequestGuard(root / 'journal')
            with guard.locked():
                guard.save({'schema_version': 1, 'phase': 'RESPONSE_ACCEPTED',
                            'attempt_id': 'a'*64, 'request_digest': 'b'*64})
            worker = root / 'worker.py'
            worker.write_text('import os,signal,socket,subprocess,sys,time\n'
                              'from pathlib import Path\n'
                              'if len(sys.argv)==1:\n'
                              ' subprocess.Popen([sys.executable,__file__,"child"])\n'
                              ' while True: time.sleep(1)\n'
                              'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
                              's=socket.socket();s.bind(("127.0.0.1",31333));s.listen()\n'
                              f'Path({str(root / "child-ready")!r}).write_text(str(os.getpid()))\n'
                              'c,_=s.accept()\n'
                              'while data:=c.recv(32): c.sendall(data)\n')
            unit.write_text('[Unit]\nDescription=Disposable WaveMesh stop CI fixture\n'
                            '[Service]\nType=simple\nRestart=no\nKillMode=control-group\n'
                            'KillSignal=SIGKILL\nSuccessExitStatus=SIGKILL\nSendSIGKILL=yes\n'
                            f'NetworkNamespacePath=/proc/{keeper.pid}/ns/net\n'
                            f'WorkingDirectory={Path("/usr/bin/python3").resolve().parent}\n'
                            f'ExecStart={Path("/usr/bin/python3").resolve()} {worker}\n'
                            'StandardOutput=null\nStandardError=null\n')
            drop_dir.mkdir()
            drop_file.write_text(drop)
            run(['systemctl', 'daemon-reload'])
            result = subprocess.run(['nsenter', '-t', str(keeper.pid), '-n', sys.executable,
                                     str(Path(__file__).resolve()), 'inner', str(root), unit_name],
                                    timeout=90, env={**os.environ, 'GITHUB_ACTIONS': 'true'})
            if result.returncode:
                raise RuntimeError('INNER_FAILED')
        finally:
            run(['systemctl', 'stop', unit_name], required=False)
            drop_file.unlink(missing_ok=True)
            if drop_dir.exists():
                drop_dir.rmdir()
            unit.unlink(missing_ok=True)
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'reset-failed', unit_name], required=False)
            keeper.kill(); keeper.communicate(timeout=5)
    print('PANEL_CONTROLLED_STOP=PASS; SCOPE=DISPOSABLE_CI_SERVICE_AND_NETWORK', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('PANEL_CONTROLLED_STOP=FAILED; TYPE=' + type(exc).__name__, file=sys.stderr)
        if isinstance(exc, (module.StopError, AssertionError, RuntimeError)) and re.fullmatch('[A-Z_]+', str(exc)):
            print('CODE=' + str(exc), file=sys.stderr)
        raise SystemExit(1)
