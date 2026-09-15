#!/usr/bin/env python3
"""Actual packaged panel/Xray stop and recovery in a disposable systemd unit."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent'))
import panel_request_guard as journal
import panel_stop
import panel_start
from panel_isolation import PanelIsolation
from xui_runtime_smoke import Smoke, SmokeFailure, Target, ThreadingHTTPServer, TARGET_PORT, PORT, command, namespace, require

OP = '00000000-0000-4000-8000-000000000001'


class UnitProcess:
    def __init__(self, unit):
        self.unit = unit

    def poll(self):
        result = subprocess.run(['systemctl', 'is-active', '--quiet', self.unit], capture_output=True, timeout=5)
        return None if result.returncode == 0 else 1


def run_inner(args):
    namespace()
    root, unit = args.root, args.unit
    smoke = Smoke(args.candidate, args.head, root / 'runtime')
    journal.DEFAULT_ROOT = smoke.root / 'panel-journal'
    journal.PANEL_UNIT = unit
    panel_start.PANEL_BINARY = smoke.binary
    panel_start.PANEL_ARGV = [str(smoke.binary)]
    guard = journal.PanelRequestGuard(journal.DEFAULT_ROOT)
    lock = smoke.root / 'node.lock'
    with guard.locked():
        guard.save({'schema_version': 1, 'phase': 'RESPONSE_ACCEPTED',
                    'attempt_id': 'a'*64, 'request_digest': 'b'*64})
    package = root / 'helper-package'
    command(['bash', '-c', 'set -Eeuo pipefail; source "$1"; wm_install_cli "$2"',
             'fixture', str(ROOT / 'scripts/00_common.sh'), str(package)])
    helper = package / 'usr/local/lib/wavemesh/lib/panel_request_guard.py'
    require(helper.read_bytes() == (ROOT / 'agent/panel_request_guard.py').read_bytes(), 'INSTALLED_HELPER_MISMATCH')
    helper.write_text(helper.read_text().replace('/var/lib/wavemesh-agent/panel-requests', str(guard.root))
                      .replace('/run/lock/wavemesh-node.lock', str(lock)))
    panel_stop.STARTUP_GUARD = helper
    helper_sha = hashlib.sha256(helper.read_bytes()).hexdigest()
    executable_sha = panel_start.file_digest(smoke.binary)
    unit_path = Path('/run/systemd/system') / unit
    dropdir = Path(str(unit_path) + '.d')
    dropfile = dropdir / '50-wavemesh-panel-startup.conf'
    require(not unit_path.exists() and not dropdir.exists(), 'UNIT_COLLISION')
    # Environment contains private fixture paths/settings, no account credentials.
    environment = ''.join('Environment="' + k + '=' + v + '"\n' for k, v in smoke.env.items())
    unit_path.write_text('[Unit]\nDescription=Disposable WaveMesh packaged recovery CI\n[Service]\n'
                         'Type=simple\nRestart=no\nKillMode=control-group\nSendSIGKILL=yes\nTimeoutStopSec=5\n'
                         f'NetworkNamespacePath={args.netns}\nWorkingDirectory={smoke.home}\n'
                         f'ExecStart={smoke.binary}\n' + environment +
                         f'StandardOutput=append:{root}/panel.private.log\n'
                         f'StandardError=append:{root}/panel.private.log\n')
    dropdir.mkdir()
    template = ROOT / 'systemd/50-wavemesh-panel-startup.conf'
    dropfile.write_text(template.read_text().replace('/usr/local/lib/wavemesh/lib/panel_request_guard.py', str(helper)))
    command(['systemctl', 'daemon-reload'])
    original_start = smoke.start
    def start(args, name):
        if name == 'panel':
            command(['systemctl', 'start', unit])
            return UnitProcess(unit)
        return original_start(args, name)
    smoke.start = start
    target_thread = None
    fds = []
    try:
        smoke.bootstrap()
        smoke.setup_clients()
        smoke.target = ThreadingHTTPServer(('127.0.0.1', TARGET_PORT), Target)
        smoke.target.seen = []
        target_thread = threading.Thread(target=smoke.target.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        target_thread.start()
        smoke.wait_traffic('control', True)
        smoke.denied_with_control()
        identities = smoke.db_state(False)
        group = Path('/sys/fs/cgroup/system.slice') / unit
        original_pids = [int(pid) for pid in (group / 'cgroup.procs').read_text().split()]
        require(len(original_pids) >= 2, 'PANEL_AND_XRAY_REQUIRED')
        fds = [os.pidfd_open(pid) for pid in original_pids]
        with journal.maintenance_node_lock(lock), guard.locked():
            guard.maintenance('prepare', OP, 1)
        PanelIsolation().isolate(guard, OP, 1, 'a'*64, 'b'*64, PORT, lock)
        with panel_stop.PanelStop().stopped(guard, OP, 1, 'a'*64, 'b'*64, PORT, helper_sha, lock):
            for fd in fds:
                poll = select.poll(); poll.register(fd, select.POLLIN)
                require(bool(poll.poll(0)), 'ORIGINAL_PANEL_OR_XRAY_SURVIVED')
            require(not smoke.traffic('control'), 'STOPPED_VPN_STILL_CONNECTS')
        print('PACKAGED_PANEL_XRAY_CGROUP_DRAIN_AND_VPN_STOP=PASS', flush=True)
        controller = panel_start.PanelStart()
        def recover():
            return controller.started(guard, OP, 1, 'a'*64, 'b'*64, PORT, helper_sha, executable_sha, lock)
        verify = controller.verify_running
        def lost_result(*values):
            verify(*values)
            raise panel_start.StartError('SYNTHETIC_LOST_RESULT')
        with patch.object(controller, 'verify_running', side_effect=lost_result):
            try:
                with recover():
                    raise SmokeFailure('LOST_RESULT_NOT_INJECTED')
            except panel_start.StartError as exc:
                if str(exc) != 'SYNTHETIC_LOST_RESULT':
                    raise
        before = (guard.root / 'state.json').read_bytes()
        with patch.object(controller, 'dispatch_start', side_effect=AssertionError('SECOND_START')):
            with patch.object(controller, 'admit', side_effect=AssertionError('SECOND_GRANT')):
                with recover():
                    smoke.login()
                    smoke.wait_traffic('control', True)
                    smoke.denied_with_control()
                    require(smoke.db_state(False) == identities, 'RECOVERY_DUPLICATED_CLIENTS')
                    path = '/panel/api/clients/update/' + smoke.clients['candidate']['email']
                    require(smoke.writers.cli(path, {**smoke.clients['candidate'], 'enable': True}).returncode != 0,
                            'CLI_WRITER_REOPENED')
                    with smoke.writers.environment():
                        try:
                            smoke.writers.agent(path, {**smoke.clients['candidate'], 'enable': True})
                            raise SmokeFailure('AGENT_WRITER_REOPENED')
                        except Exception as exc:
                            # Assert the actual transport's typed denial, not any error.
                            from xui_writer_smoke import runtime
                            require(isinstance(exc, runtime.ProvisionError) and str(exc) == 'Node mutation is busy',
                                    'AGENT_DENIAL_UNPROVEN')
        require((guard.root / 'state.json').read_bytes() == before, 'REPLAY_CHANGED_JOURNAL')
        print('PACKAGED_RECOVERY_LOST_RESULT_RECONCILED_ONCE=PASS', flush=True)
        print('RECOVERED_REAL_VLESS_AND_IDENTITIES_WITH_WRITERS_CLOSED=PASS', flush=True)
        report = {'schema': 1, 'builder_commit': args.head, 'status': 'PASS',
                  'scope': 'DISPOSABLE_SYSTEMD_PRIVATE_NETWORK', 'deployment': 'NONE',
                  'panel_sha256': executable_sha, 'archive_sha256': smoke.manifest['archive_sha256']}
        args.report.write_text(json.dumps(report, sort_keys=True) + '\n')
    finally:
        subprocess.run(['systemctl', 'stop', unit], capture_output=True, timeout=30)
        if target_thread:
            smoke.target.shutdown(); smoke.target.server_close(); target_thread.join(timeout=2)
        smoke.close()
        for fd in fds:
            os.close(fd)
        dropfile.unlink(missing_ok=True)
        dropdir.rmdir()
        unit_path.unlink(missing_ok=True)
        command(['systemctl', 'daemon-reload'])
        subprocess.run(['systemctl', 'reset-failed', unit], capture_output=True, timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--head', required=True)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--unit')
    parser.add_argument('--netns')
    args = parser.parse_args()
    require(sys.platform == 'linux' and os.geteuid() == 0 and os.environ.get('GITHUB_ACTIONS') == 'true', 'CI_ROOT_REQUIRED')
    if args.root:
        run_inner(args)
        return
    require(Path('/proc/1/comm').read_text().strip() == 'systemd', 'SYSTEMD_REQUIRED')
    with tempfile.TemporaryDirectory(prefix='wm-real-recover-', dir='/run') as directory:
        root = Path(directory)
        (root / 'runtime').mkdir()
        parent_namespace = os.readlink('/proc/self/ns/net')
        keeper = subprocess.Popen(['unshare', '--net', sys.executable, '-c',
                                   'import sys;print("READY",flush=True);sys.stdin.read()'],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([keeper.stdout], [], [], 5)
            require(ready and keeper.stdout.readline().strip() == 'READY', 'NAMESPACE_NOT_READY')
            netns = '/proc/' + str(keeper.pid) + '/ns/net'
            result = subprocess.run(['nsenter', '-t', str(keeper.pid), '-n', sys.executable,
                                     str(Path(__file__).resolve()), '--candidate', str(args.candidate.resolve()),
                                     '--head', args.head, '--report', str(args.report.resolve()),
                                     '--root', str(root), '--unit', root.name + '.service', '--netns', netns],
                                    env={**os.environ, 'WAVEMESH_CI_PARENT_NETNS': parent_namespace}, timeout=240)
            require(result.returncode == 0, 'INNER_RECOVERY_FAILED')
        finally:
            keeper.kill(); keeper.communicate(timeout=5)


if __name__ == '__main__':
    try:
        os.umask(0o077)
        main()
    except Exception as exc:
        print('PACKAGED_SYSTEMD_RECOVERY=FAILED; TYPE=' + type(exc).__name__, file=sys.stderr)
        if isinstance(exc, (SmokeFailure, panel_stop.StopError, panel_start.StartError)) and re.fullmatch('[A-Z_]+', str(exc)):
            print('CODE=' + str(exc), file=sys.stderr)
        raise SystemExit(1)
