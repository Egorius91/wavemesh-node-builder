"""Actual recovery of stopped and killed maintenance invocations in CI."""
import json
import time
from unittest.mock import patch
from urllib import request, error

import panel_maintenance_recovery
import panel_start
from xui_runtime_smoke import command, require, SmokeFailure, PORT


def run(smoke, guard, lock, unit, operation, candidate_sha, rollback_sha,
        manifest_sha, head, helper_sha, executable_sha):
    controller = panel_maintenance_recovery.PanelMaintenanceRecovery(manifest_sha, head)
    identities = smoke.db_state(False)
    browser = request.build_opener(request.ProxyHandler({}))
    token = smoke.writers.config['panel']['api_auth']['token']
    def status():
        req = request.Request('http://127.0.0.1:' + str(PORT) + '/smoke/panel/api/wavemesh/maintenance',
                              headers={'Authorization': 'Bearer ' + token})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with browser.open(req, timeout=3) as response:
                    raw = response.read(4097)
                    require(len(raw) <= 4096, 'RECOVERY_STATUS_LIMIT')
                    value = json.loads(raw)
                    require(response.code == 200 and value.get('success') is True and value.get('obj') == {
                        'protocol': 'wavemesh-maintenance-v1', 'maintenance': True}, 'RECOVERY_STATUS_INVALID')
                    return
            except (error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.1)
        raise SmokeFailure('RECOVERY_STATUS_TIMEOUT')
    attempts = ['00000000-0000-4000-8000-000000000020', '00000000-0000-4000-8000-000000000021']
    old_invocations = []
    for index, attempt in enumerate(attempts):
        if index:
            command(['systemctl', 'kill', '--kill-whom=main', '--signal=KILL', unit])
            deadline = time.monotonic() + 10
            while controller.observe()['ActiveState'] != 'failed' and time.monotonic() < deadline:
                time.sleep(0.1)
            require(controller.observe()['ActiveState'] == 'failed', 'RECOVERY_FAILURE_NOT_OBSERVED')
        previous = guard.load()['start']['invocation_id']
        old_invocations.append(previous)
        def recover(identity=attempt, prior=previous):
            return controller.recovered(guard, operation, 1, candidate_sha, rollback_sha, PORT,
                                        helper_sha, executable_sha, identity, prior, lock)
        verify = controller.verify_running
        def lost(*values):
            verify(*values)
            raise panel_start.StartError('SYNTHETIC_LOST_RESULT')
        with patch.object(controller, 'verify_running', side_effect=lost):
            try:
                with recover():
                    raise SmokeFailure('RECOVERY_LOST_RESULT_NOT_INJECTED')
            except panel_start.StartError as exc:
                require(str(exc) == 'SYNTHETIC_LOST_RESULT', 'RECOVERY_START_FAILED')
        state = guard.load()
        require(state['schema_version'] == 8 and len(state['recoveries']) == index + 1
                and state['start']['invocation_id'] not in old_invocations, 'RECOVERY_HISTORY_INVALID')
        before = (guard.root / 'state.json').read_bytes()
        with patch.object(controller, 'dispatch_start', side_effect=AssertionError('SECOND_RECOVERY_START')):
            with patch.object(controller, 'admit', side_effect=AssertionError('SECOND_RECOVERY_GRANT')):
                with recover() as receipt:
                    require(receipt['reconciliation_required'], 'RECOVERY_REPLAY_NOT_IDENTIFIED')
                    status()
                    require(not smoke.traffic('control') and not smoke.traffic('candidate'), 'RECOVERY_EXPOSED_VPN')
                # A new ID never permits restarting a currently running process.
                try:
                    with recover('00000000-0000-4000-8000-000000000099', state['start']['invocation_id']):
                        raise SmokeFailure('RECOVERY_ACTIVE_RESTART_ALLOWED')
                except panel_start.StartError as exc:
                    require(str(exc) == 'RECOVERY_TERMINAL_REQUIRED', 'RECOVERY_ACTIVE_DENIAL_UNPROVEN')
                if index:
                    try:
                        with recover(attempts[0], old_invocations[0]):
                            raise SmokeFailure('RECOVERY_STALE_ID_ALLOWED')
                    except panel_start.StartError as exc:
                        require(str(exc) == 'RECOVERY_ATTEMPT_CONFLICT', 'RECOVERY_STALE_DENIAL_UNPROVEN')
        require((guard.root / 'state.json').read_bytes() == before, 'RECOVERY_REPLAY_MUTATED_JOURNAL')
    require(smoke.db_state(False) == identities, 'RECOVERY_CLIENT_IDENTITIES_CHANGED')
    path = '/panel/api/clients/update/' + smoke.clients['candidate']['email']
    require(smoke.writers.cli(path, {**smoke.clients['candidate'], 'enable': True}).returncode != 0,
            'RECOVERY_CLI_WRITER_ALLOWED')
    with smoke.writers.environment():
        from xui_writer_smoke import runtime
        try:
            smoke.writers.agent(path, {**smoke.clients['candidate'], 'enable': True})
            raise SmokeFailure('RECOVERY_AGENT_WRITER_ALLOWED')
        except runtime.ProvisionError as exc:
            require(str(exc) == 'PANEL_LOCAL_MAINTENANCE_HELD', 'RECOVERY_AGENT_DENIAL_UNPROVEN')
    print('TERMINAL_STOPPED_AND_FAILED_MAINTENANCE_RECOVERY=PASS', flush=True)
    print('RECOVERY_LOST_RESULTS_AND_STALE_IDS_NO_DUPLICATE_START=PASS', flush=True)
    print('RECOVERY_RETAINS_IDENTITIES_AND_VPN_WRITER_DENIAL=PASS', flush=True)
