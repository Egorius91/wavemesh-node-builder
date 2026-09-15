#!/usr/bin/env python3
"""Actual packaged panel/Xray stop and recovery in a disposable systemd unit."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
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
import panel_backup
import panel_candidate
import panel_replace
from panel_isolation import PanelIsolation
from xui_runtime_smoke import Smoke, SmokeFailure, Target, ThreadingHTTPServer, TARGET_PORT, PORT, command, namespace, require

OP = '00000000-0000-4000-8000-000000000001'


class UnitProcess:
    def __init__(self, unit):
        self.unit = unit

    def poll(self):
        result = subprocess.run(['systemctl', 'is-active', '--quiet', self.unit], capture_output=True, timeout=5)
        return None if result.returncode == 0 else 1


def replacement_smoke(smoke, guard, lock, unit, candidate_sha, rollback_sha, manifest_sha, head, helper_sha, executable_sha):
    controller = panel_replace.PanelReplacement()
    original = panel_replace.describe(smoke.home)
    database_before = hashlib.sha256(smoke.db.read_bytes()).hexdigest()
    arguments = (guard, OP, 1, candidate_sha, rollback_sha, manifest_sha, head, PORT, helper_sha, lock)
    actual_exchange = panel_replace.exchange
    def lost(*paths):
        actual_exchange(*paths)
        raise panel_replace.ReplacementError('SYNTHETIC_LOST_RESULT')
    for action, pending, terminal in (('replace', 'REPLACE_INTENT', 'REPLACED'),
                                      ('rollback', 'ROLLBACK_INTENT', 'ROLLED_BACK')):
        with patch.object(panel_replace, 'exchange', side_effect=lost) as call:
            try:
                controller.run(action, *arguments)
                raise SmokeFailure('FILE_LOST_RESULT_NOT_INJECTED')
            except panel_replace.ReplacementError as exc:
                require(str(exc) == 'SYNTHETIC_LOST_RESULT', 'FILE_EXCHANGE_FAILED')
            require(call.call_count == 1, 'FILE_EXCHANGE_COUNT_INVALID')
        require(guard.load()['replacement']['phase'] == pending, 'FILE_INTENT_NOT_RETAINED')
        with patch.object(panel_replace, 'exchange', side_effect=AssertionError('SECOND_EXCHANGE')):
            require(controller.run(action, *arguments)['files'] == terminal, 'FILE_RECONCILIATION_FAILED')
        if action == 'replace':
            require(smoke.home.stat().st_ino != original['inode'], 'FILE_TREE_NOT_EXCHANGED')
            require(not (smoke.home / '.wm-original-tree-proof').exists(), 'ORIGINAL_TREE_STILL_LIVE')
            require(panel_start.file_digest(smoke.binary) == executable_sha, 'REPLACEMENT_BINARY_MISMATCH')
            require(panel_replace.describe(panel_replace.location(OP, 1) / 'slot') == original, 'ORIGINAL_TREE_NOT_RETAINED')
        else:
            require(panel_replace.describe(smoke.home) == original, 'FILE_ROLLBACK_MISMATCH')
        require(not smoke.traffic('control'), 'FILE_TRANSACTION_EXPOSED_VPN')
    require(hashlib.sha256(smoke.db.read_bytes()).hexdigest() == database_before, 'FILE_TRANSACTION_CHANGED_DATABASE')
    with patch.object(panel_start.PanelStart, 'dispatch_start', side_effect=AssertionError('UNSAFE_START')):
        try:
            with panel_start.PanelStart().started(guard, OP, 1, candidate_sha, rollback_sha, PORT, helper_sha, executable_sha, lock):
                raise SmokeFailure('RECOVERY_BYPASSED_FILE_JOURNAL')
        except panel_start.StartError as exc:
            require(str(exc) == 'START_STOP_PROOF_REQUIRED', 'RECOVERY_FILE_DENIAL_UNPROVEN')
    result = subprocess.run(['systemctl', 'start', unit], capture_output=True, timeout=30)
    require(result.returncode != 0 and panel_stop.PanelStop().observe()['MainPID'] == '0', 'ORDINARY_START_BYPASSED_FILE_JOURNAL')
    path = '/panel/api/clients/update/' + smoke.clients['candidate']['email']
    require(smoke.writers.cli(path, {**smoke.clients['candidate'], 'enable': True}).returncode != 0, 'FILE_JOURNAL_CLI_WRITE_ALLOWED')
    with smoke.writers.environment():
        from xui_writer_smoke import runtime
        try:
            smoke.writers.agent(path, {**smoke.clients['candidate'], 'enable': True})
            raise SmokeFailure('FILE_JOURNAL_AGENT_WRITE_ALLOWED')
        except runtime.ProvisionError as exc:
            # run() has released volatile locks. The durable hold, not flock
            # contention or an unavailable panel socket, must deny this writer.
            require(str(exc) == 'PANEL_LOCAL_MAINTENANCE_HELD', 'FILE_JOURNAL_AGENT_DENIAL_UNPROVEN')
    print('REAL_PANEL_TREE_EXCHANGE_AND_ROLLBACK_AFTER_LOST_RESULTS=PASS', flush=True)
    print('FILE_TRANSACTION_PRESERVES_DATABASE_AND_DENIES_STARTUP_AND_WRITERS=PASS', flush=True)


def run_inner(args):
    namespace()
    root, unit = args.root, args.unit
    smoke = Smoke(args.candidate, args.head, root / 'runtime')
    journal.DEFAULT_ROOT = smoke.root / 'panel-journal'
    journal.PANEL_UNIT = unit
    panel_start.PANEL_BINARY = smoke.binary
    panel_start.PANEL_ARGV = [str(smoke.binary)]
    panel_backup.PANEL_HOME = smoke.home
    panel_backup.PANEL_DB = smoke.db
    panel_candidate.PANEL_HOME = smoke.home
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
                      .replace('/run/lock/wavemesh-node.lock', str(lock))
                      .replace('PANEL_UNIT = "x-ui.service"', 'PANEL_UNIT = ' + json.dumps(unit)))
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
        # Every independent guard copy must validate this disposable unit, not
        # production x-ui.service. Otherwise writers reject a valid fixture
        # stop journal as PANEL_STOP_INVALID instead of proving durable HELD.
        from xui_writer_smoke import runtime
        runtime._guard_module.PANEL_UNIT = unit
        cli_guard = smoke.writers.library / 'lib/panel_request_guard.py'
        source = cli_guard.read_text()
        require(source.count('PANEL_UNIT = "x-ui.service"') == 1, 'CLI_UNIT_RELOCATION_UNPROVEN')
        cli_guard.write_text(source.replace('PANEL_UNIT = "x-ui.service"', 'PANEL_UNIT = ' + json.dumps(unit)))
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
        # CI is the trusted producer here, not a production promotion service.
        # Transfer its verified bundle into a root-owned private incoming area.
        manifest_sha = hashlib.sha256((args.candidate / 'manifest.json').read_bytes()).hexdigest()
        incoming = root / 'incoming'
        incoming.mkdir(mode=0o700)
        for name in ('manifest.json', 'source.tar.gz', panel_candidate.ARCHIVE):
            shutil.copyfile(args.candidate / name, incoming / name)
            (incoming / name).chmod(0o600)
        prepared = panel_candidate.prepare(guard, OP, 1, incoming, manifest_sha, args.head, lock)
        require(prepared['panel_sha256'] == executable_sha, 'PREPARED_PANEL_MISMATCH')
        with patch.object(panel_candidate, 'extract', side_effect=AssertionError('SECOND_EXTRACTION')):
            replay = panel_candidate.prepare(guard, OP, 1, incoming, manifest_sha, args.head, lock)
            require(replay['reconciliation_required'] and replay['candidate_sha256'] == prepared['candidate_sha256'],
                    'CANDIDATE_REPLAY_UNPROVEN')
        candidate_sha = prepared['candidate_sha256']
        if args.replacement:
            marker = smoke.home / '.wm-original-tree-proof'
            marker.write_bytes(b'original tree fixture')
            marker.chmod(0o600)
        backup = panel_backup.prepare(guard, OP, 1, candidate_sha, lock)
        rollback_sha = backup['rollback_manifest_sha256']
        snapshot = panel_backup.location(guard, OP, 1)
        # Verify actual private fixture identities/disabled state without logging
        # them. This is a read of the snapshot, never a live DB restoration.
        with patch.object(smoke, 'db', snapshot / 'database.sqlite'):
            require(smoke.db_state(False) == identities, 'SNAPSHOT_CLIENT_IDENTITY_MISMATCH')
        PanelIsolation().isolate(guard, OP, 1, candidate_sha, rollback_sha, PORT, lock)
        with panel_stop.PanelStop().stopped(guard, OP, 1, candidate_sha, rollback_sha, PORT, helper_sha, lock):
            panel_candidate.verify_locked(guard, OP, 1, manifest_sha, args.head)
            panel_backup.verify_locked(guard, OP, 1, candidate_sha, rollback_sha)
            for fd in fds:
                poll = select.poll(); poll.register(fd, select.POLLIN)
                require(bool(poll.poll(0)), 'ORIGINAL_PANEL_OR_XRAY_SURVIVED')
            require(not smoke.traffic('control'), 'STOPPED_VPN_STILL_CONNECTS')
        print('PACKAGED_PANEL_XRAY_CGROUP_DRAIN_AND_VPN_STOP=PASS', flush=True)
        print('OPERATION_BOUND_SQLITE_AND_PANEL_SNAPSHOT=PASS', flush=True)
        print('TRUST_BOUND_CANDIDATE_PREPARATION_AND_REPLAY=PASS', flush=True)
        if args.replacement:
            replacement_smoke(smoke, guard, lock, unit, candidate_sha, rollback_sha, manifest_sha,
                              args.head, helper_sha, executable_sha)
            report = {'schema': 1, 'builder_commit': args.head, 'status': 'PASS', 'deployment': 'NONE',
                      'scope': 'DISPOSABLE_STOPPED_FILE_TRANSACTION', 'file_replacement_verified': True,
                      'file_rollback_verified': True, 'database_unchanged': True, 'startup': 'DENIED',
                      'candidate_manifest_sha256': manifest_sha, 'panel_sha256': executable_sha,
                      'archive_sha256': candidate_sha}
            args.report.write_text(json.dumps(report, sort_keys=True) + '\n')
            args.report.chmod(0o644)
            return
        controller = panel_start.PanelStart()
        def recover():
            return controller.started(guard, OP, 1, candidate_sha, rollback_sha, PORT, helper_sha, executable_sha, lock)
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
                  'rollback_snapshot_verified': True,
                  'candidate_preparation_verified': True, 'candidate_manifest_sha256': manifest_sha,
                  'panel_sha256': executable_sha, 'archive_sha256': smoke.manifest['archive_sha256']}
        args.report.write_text(json.dumps(report, sort_keys=True) + '\n')
        # Only this allow-listed digest/status report is public CI evidence.
        # Private fixture logs, database and credentials keep the 0077 umask.
        args.report.chmod(0o644)
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
    parser.add_argument('--replacement', action='store_true')
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
                                     '--root', str(root), '--unit', root.name + '.service', '--netns', netns,
                                     *(['--replacement'] if args.replacement else [])],
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
        if isinstance(exc, (SmokeFailure, panel_stop.StopError, panel_start.StartError,
                            panel_backup.BackupError, panel_candidate.CandidateError,
                            panel_replace.ReplacementError)) and re.fullmatch('[A-Z_]+', str(exc)):
            print('CODE=' + str(exc), file=sys.stderr)
        raise SystemExit(1)
