#!/usr/bin/env python3
"""Actual packaged maintenance API and denied VPN in a private CI network."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from urllib import error, request

from xui_runtime_smoke import Smoke, SmokeFailure, ThreadingHTTPServer, Target, TARGET_PORT, PORT, namespace, require


def run(smoke):
    smoke.bootstrap()
    smoke.setup_clients()
    target = ThreadingHTTPServer(('127.0.0.1', TARGET_PORT), Target)
    target.seen = []
    smoke.target = target
    thread = threading.Thread(target=target.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    try:
        smoke.denied_with_control()
        before = smoke.db_state(False)
        smoke.stop(smoke.panel)
        smoke.processes.remove(smoke.panel)
        smoke.env['WAVEMESH_PANEL_MODE'] = 'maintenance'
        smoke.panel = smoke.start([str(smoke.binary)], 'maintenance-panel')
        token = smoke.writers.config['panel']['api_auth']['token']
        browser = request.build_opener(request.ProxyHandler({}))

        def call(path, method='GET', authenticated=True, payload=None):
            headers = {'X-Requested-With': 'XMLHttpRequest'}
            if authenticated:
                headers['Authorization'] = 'Bearer ' + token
            data = None if payload is None else json.dumps(payload).encode()
            if data is not None:
                headers['Content-Type'] = 'application/json'
            req = request.Request('http://127.0.0.1:' + str(PORT) + '/smoke/' + path,
                                  method=method, headers=headers, data=data)
            try:
                response = browser.open(req, timeout=3)
            except error.HTTPError as exc:
                response = exc
            with response:
                raw = response.read(4 * 1024 * 1024 + 1)
                require(len(raw) <= 4 * 1024 * 1024, 'MAINTENANCE_RESPONSE_LIMIT')
                return response.code, response.headers.get('X-WaveMesh-Panel-Mode'), raw

        def ready():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                require(smoke.panel.poll() is None, 'MAINTENANCE_PANEL_EXITED')
                try:
                    code, mode, raw = call('panel/api/wavemesh/maintenance')
                    value = json.loads(raw)
                    require(code == 200 and mode == 'maintenance-v1'
                            and value.get('success') is True
                            and value.get('obj') == {'protocol': 'wavemesh-maintenance-v1', 'maintenance': True},
                            'MAINTENANCE_MODE_UNPROVEN')
                    return
                except (error.URLError, TimeoutError, ConnectionError):
                    time.sleep(0.2)
            raise SmokeFailure('MAINTENANCE_START_TIMEOUT')

        ready()
        require(call('panel/api/clients/list', authenticated=False)[0] == 401, 'MAINTENANCE_AUTH_BYPASS')
        code, _, raw = call('panel/api/clients/list')
        value = json.loads(raw)
        require(code == 200 and value.get('success') is True and len(value.get('obj', [])) == 2,
                'MAINTENANCE_INVENTORY_UNAVAILABLE')
        for method, path, payload in (
            ('POST', 'panel/api/server/restartXray', {}),
            ('POST', 'panel/api/clients/update/' + smoke.clients['candidate']['email'], {**smoke.clients['candidate'], 'enable': True}),
            ('POST', 'panel/api/setting/update', {'WAVEMESH_PANEL_MODE': ''}),
            ('GET', 'panel/api/server/getXrayVersion', None),
            ('GET', 'ws', None), ('POST', 'login', {})):
            code, mode, raw = call(path, method, payload=payload)
            require(code == 503 and mode == 'maintenance-v1'
                    and json.loads(raw).get('msg') == 'WAVEMESH_MAINTENANCE_READ_ONLY',
                    'MAINTENANCE_WRITE_OR_ACTIVATION_ADMITTED')
        # Exercise the real panel-only SIGHUP restart path without changing mode.
        smoke.panel.send_signal(signal.SIGHUP)
        time.sleep(0.4)
        ready()
        deadline = time.monotonic() + 16
        while time.monotonic() < deadline:
            result = subprocess.run(['ps', '-e', '-o', 'pid=,pgid='], capture_output=True, timeout=3, check=True)
            group = [int(row[0]) for line in result.stdout.splitlines() if len(row := line.split()) == 2
                     and int(row[1]) == smoke.panel.pid]
            require(group == [smoke.panel.pid], 'MAINTENANCE_CHILD_PROCESS_PRESENT')
            require(not smoke.traffic('control') and not smoke.traffic('candidate'), 'MAINTENANCE_VPN_EXPOSED')
            time.sleep(0.2)
        require(smoke.db_state(False) == before, 'MAINTENANCE_CHANGED_CLIENT_IDENTITIES')
        print('MAINTENANCE_AUTHENTICATED_INVENTORY_WITHOUT_XRAY=PASS', flush=True)
        print('MAINTENANCE_DENIES_WRITES_RESTART_AND_REAL_VLESS=PASS', flush=True)
        print('MAINTENANCE_SURVIVES_PANEL_ONLY_RESTART=PASS', flush=True)
    finally:
        target.shutdown(); target.server_close(); thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    namespace()
    with tempfile.TemporaryDirectory(prefix='wm-maintenance-') as directory:
        smoke = Smoke(args.candidate.resolve(), args.head, Path(directory))
        try:
            run(smoke)
            value = {'schema': 1, 'status': 'PASS', 'builder_commit': args.head, 'deployment': 'NONE',
                     'scope': 'PRIVATE_MAINTENANCE_BACKEND', 'authenticated_inventory': True,
                     'runtime_activation_denied': True, 'real_vless_denied': True,
                     'panel_only_restart_stays_maintenance': True, 'client_identities_unchanged': True,
                     'archive_sha256': smoke.manifest['archive_sha256'],
                     'panel_sha256': smoke.manifest['members']['x-ui/x-ui']['sha256']}
            args.report.write_text(json.dumps(value, sort_keys=True) + '\n')
            args.report.chmod(0o644)
        finally:
            smoke.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('MAINTENANCE_SMOKE=FAILED; TYPE=' + type(exc).__name__)
        if isinstance(exc, SmokeFailure):
            print('CODE=' + str(exc))
        raise SystemExit(1)
