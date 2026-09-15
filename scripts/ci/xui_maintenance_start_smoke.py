"""Checks called inside the disposable packaged systemd recovery fixture."""
import json
from pathlib import Path
import time
from unittest.mock import patch
from urllib import error, request

import panel_maintenance_start
import panel_replace
import panel_start
from xui_runtime_smoke import command, require, SmokeFailure, PORT


def run(smoke, guard, lock, unit_path, unit, operation, candidate_sha, rollback_sha,
        manifest_sha, head, helper_sha, executable_sha):
    identities = smoke.db_state(False)
    result = panel_replace.PanelReplacement().run('replace', guard, operation, 1, candidate_sha,
        rollback_sha, manifest_sha, head, PORT, helper_sha, lock)
    require(result['files'] == 'REPLACED', 'MAINTENANCE_REPLACEMENT_MISSING')
    controller = panel_maintenance_start.PanelMaintenanceStart(manifest_sha, head)
    def start():
        return controller.started(guard, operation, 1, candidate_sha, rollback_sha, PORT,
                                  helper_sha, executable_sha, lock)
    before = (guard.root / 'state.json').read_bytes()
    with patch.object(controller, 'dispatch_start', side_effect=AssertionError('START_WITHOUT_MODE')):
        try:
            with start():
                raise SmokeFailure('MAINTENANCE_MODE_NOT_REQUIRED')
        except panel_start.StartError as exc:
            require(str(exc) == 'MAINTENANCE_MODE_REQUIRED', 'MISSING_MODE_DENIAL_UNPROVEN')
    require((guard.root / 'state.json').read_bytes() == before, 'MISSING_MODE_CHANGED_JOURNAL')
    # Disposable fixture only. The product controller never edits unit settings.
    unit_path.write_text(unit_path.read_text() + '\nEnvironment=WAVEMESH_PANEL_MODE=maintenance\n')
    command(['systemctl', 'daemon-reload'])
    verify = controller.verify_running
    def lost(*args):
        verify(*args)
        raise panel_start.StartError('SYNTHETIC_LOST_RESULT')
    with patch.object(controller, 'verify_running', side_effect=lost):
        try:
            with start():
                raise SmokeFailure('MAINTENANCE_LOST_RESULT_NOT_INJECTED')
        except panel_start.StartError as exc:
            require(str(exc) == 'SYNTHETIC_LOST_RESULT', 'MAINTENANCE_START_FAILED')
    state = guard.load()
    require(state['schema_version'] == 7 and state['start']['phase'] == 'START_ADMITTED',
            'MAINTENANCE_ADMISSION_NOT_DURABLE')
    before = (guard.root / 'state.json').read_bytes()
    token = smoke.writers.config['panel']['api_auth']['token']
    browser = request.build_opener(request.ProxyHandler({}))
    def call(path, method='GET', authenticated=True, payload=None):
        headers = {'X-Requested-With': 'XMLHttpRequest'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + token
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        req = request.Request('http://127.0.0.1:' + str(PORT) + '/smoke/' + path,
                              method=method, headers=headers,
                              data=None if payload is None else json.dumps(payload).encode())
        try:
            response = browser.open(req, timeout=3)
        except error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read(4 * 1024 * 1024 + 1)
            require(len(raw) <= 4 * 1024 * 1024, 'MAINTENANCE_RESPONSE_LIMIT')
            return response.code, json.loads(raw)
    def ready():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                code, value = call('panel/api/wavemesh/maintenance')
                require(code == 200 and value.get('success') is True and value.get('obj') == {
                    'protocol': 'wavemesh-maintenance-v1', 'maintenance': True}, 'MAINTENANCE_STATUS_INVALID')
                return
            except (error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.1)
        raise SmokeFailure('MAINTENANCE_STATUS_TIMEOUT')
    with patch.object(controller, 'dispatch_start', side_effect=AssertionError('SECOND_START')):
        with patch.object(controller, 'admit', side_effect=AssertionError('SECOND_GRANT')):
            with start() as result:
                require(result['activation'] == 'MAINTENANCE_BOUND_INVOCATION', 'MAINTENANCE_RECEIPT_INVALID')
                ready()
                require(call('panel/api/clients/list', authenticated=False)[0] == 401, 'MAINTENANCE_AUTH_BYPASS')
                code, inventory = call('panel/api/clients/list')
                require(code == 200 and inventory.get('success') is True and len(inventory.get('obj', [])) == 2,
                        'MAINTENANCE_INVENTORY_FAILED')
                for path in ('panel/api/server/restartXray', 'panel/api/clients/update/' + smoke.clients['candidate']['email']):
                    code, value = call(path, 'POST', payload={**smoke.clients['candidate'], 'enable': True})
                    require(code == 503 and value.get('msg') == 'WAVEMESH_MAINTENANCE_READ_ONLY', 'MAINTENANCE_WRITE_ALLOWED')
                observed = controller.observe()
                command(['systemctl', 'kill', '--kill-whom=main', '--signal=HUP', unit])
                time.sleep(0.4)
                ready()
                deadline = time.monotonic() + 16
                while time.monotonic() < deadline:
                    pids = (Path('/sys/fs/cgroup/system.slice') / unit / 'cgroup.procs').read_text().split()
                    require(pids == [observed['MainPID']], 'MAINTENANCE_CHILD_PRESENT')
                    require(not smoke.traffic('control') and not smoke.traffic('candidate'), 'MAINTENANCE_VPN_EXPOSED')
                    time.sleep(0.2)
                controller.verify_running(guard.load(), helper_sha, executable_sha)
    require((guard.root / 'state.json').read_bytes() == before, 'MAINTENANCE_REPLAY_CHANGED_JOURNAL')
    require(smoke.db_state(False) == identities, 'MAINTENANCE_IDENTITIES_CHANGED')
    # Volatile locks are released: prove durable HELD still blocks both writers.
    path = '/panel/api/clients/update/' + smoke.clients['candidate']['email']
    require(smoke.writers.cli(path, {**smoke.clients['candidate'], 'enable': True}).returncode != 0, 'MAINTENANCE_CLI_ALLOWED')
    with smoke.writers.environment():
        from xui_writer_smoke import runtime
        try:
            smoke.writers.agent(path, {**smoke.clients['candidate'], 'enable': True})
            raise SmokeFailure('MAINTENANCE_AGENT_ALLOWED')
        except runtime.ProvisionError as exc:
            require(str(exc) == 'PANEL_LOCAL_MAINTENANCE_HELD', 'MAINTENANCE_AGENT_DENIAL_UNPROVEN')
    # Stopping does not restore ordinary admission. No new StartUnit job allowed.
    command(['systemctl', 'stop', unit])
    with patch.object(controller, 'dispatch_start', side_effect=AssertionError('SECOND_START')):
        try:
            with start():
                raise SmokeFailure('MAINTENANCE_SECOND_ACTIVATION_ALLOWED')
        except panel_start.StartError as exc:
            require(str(exc) == 'START_RECONCILIATION_REQUIRED', 'MAINTENANCE_STOP_DENIAL_UNPROVEN')
    print('REPLACED_PANEL_MAINTENANCE_ONE_JOB_LOST_RESULT_RECONCILED=PASS', flush=True)
    print('MAINTENANCE_AUTH_READS_DENIED_VPN_WRITERS_AND_SECOND_START=PASS', flush=True)
