#!/usr/bin/env python3
"""Actual systemd admission; only a dedicated, disposable CI service is touched."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
from panel_request_guard import PanelRequestGuard, maintenance_node_lock

OP = "00000000-0000-4000-8000-000000000001"
ACCEPTED = {"schema_version": 1, "phase": "RESPONSE_ACCEPTED",
            "attempt_id": "a" * 64, "request_digest": "b" * 64}


def run(args, required=True, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=20, **kwargs)
    if required and result.returncode:
        raise RuntimeError("CI_FIXTURE_COMMAND_FAILED")
    return result


def main():
    if sys.platform != "linux" or os.geteuid() != 0 or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("DEDICATED_GITHUB_LINUX_ROOT_REQUIRED")
    if Path('/proc/1/comm').read_text().strip() != 'systemd':
        raise RuntimeError("SYSTEMD_REQUIRED")
    with tempfile.TemporaryDirectory(prefix='wm-startup-ci-', dir='/run') as directory:
        root = Path(directory)
        unit_name = root.name + '.service'
        unit = Path('/run/systemd/system') / unit_name
        drop_dir = Path(str(unit) + '.d')
        drop_file = drop_dir / '50-wavemesh-panel-startup.conf'
        # Refuse collisions; never replace an existing runner service/drop-in.
        if unit.exists() or unit.is_symlink() or drop_dir.exists() or drop_dir.is_symlink():
            raise RuntimeError('FIXTURE_UNIT_COLLISION')
        marker = root / 'exec-start-ran'
        guard = PanelRequestGuard(root / 'journal')
        node_lock = root / 'node.lock'
        package = root / 'package'
        run(['bash', '-c', 'set -Eeuo pipefail; source "$1"; wm_install_cli "$2"',
             'fixture', str(ROOT / 'scripts/00_common.sh'), str(package)])
        installed = package / 'usr/local/lib/wavemesh/lib/panel_request_guard.py'
        assert installed.read_bytes() == (ROOT / 'agent/panel_request_guard.py').read_bytes()
        # Relocate only fixed paths in the private installed fixture. Production
        # code offers no configurable startup journal/lock bypass.
        source = installed.read_text()
        assert source.count('/var/lib/wavemesh-agent/panel-requests') == 1
        source = source.replace('/var/lib/wavemesh-agent/panel-requests', str(guard.root))
        source = source.replace('/run/lock/wavemesh-node.lock', str(node_lock))
        installed.write_text(source)
        template = package / 'usr/local/lib/wavemesh/systemd/50-wavemesh-panel-startup.conf'
        assert template.read_bytes() == (ROOT / 'systemd/50-wavemesh-panel-startup.conf').read_bytes()
        drop = template.read_text().replace('/usr/local/lib/wavemesh/lib/panel_request_guard.py', str(installed))
        decoy = PanelRequestGuard(root / 'decoy')
        with decoy.locked():
            decoy.save(ACCEPTED)
        try:
            unit.write_text('[Unit]\nDescription=Disposable WaveMesh startup CI fixture\n'
                            '[Service]\nType=oneshot\nRestart=no\n'
                            f'Environment=WAVEMESH_PANEL_REQUEST_STATE_DIR={decoy.root}\n'
                            f'ExecStart=/usr/bin/touch {marker}\n'
                            'StandardOutput=null\nStandardError=null\n', encoding='utf-8')
            drop_dir.mkdir()
            drop_file.write_text(drop, encoding='utf-8')
            run(['systemctl', 'daemon-reload'])

            def attempt(allowed):
                marker.unlink(missing_ok=True)
                before = (guard.root / 'state.json').read_bytes() if (guard.root / 'state.json').exists() else None
                run(['systemctl', 'reset-failed', unit_name], required=False)
                result = run(['systemctl', 'start', unit_name], required=False)
                assert (result.returncode == 0) == allowed, 'START_RESULT_MISMATCH'
                assert marker.exists() == allowed, 'EXEC_START_GATE_BYPASSED'
                after = (guard.root / 'state.json').read_bytes() if (guard.root / 'state.json').exists() else None
                assert after == before, 'STARTUP_CHANGED_STATE'
                if not allowed:
                    state = run(['systemctl', 'show', unit_name, '--property=Result', '--value']).stdout.strip()
                    assert state == b'exit-code', 'FAILURE_NOT_FROM_GUARD'

            def save(value):
                with guard.locked():
                    guard.save(value)

            attempt(False)
            assert not guard.root.exists()
            save(ACCEPTED)
            attempt(True)
            print('SYSTEMD_ACCEPTED_START_AND_MISSING_STATE=PASS', flush=True)
            save({**ACCEPTED, 'phase': 'DISPATCH_INTENT'})
            attempt(False)
            save(ACCEPTED)
            with maintenance_node_lock(node_lock), guard.locked():
                guard.maintenance('prepare', OP, 1)
            attempt(False)
            with maintenance_node_lock(node_lock), guard.locked():
                guard.maintenance('cancel', OP, 1)
            attempt(True)
            with maintenance_node_lock(node_lock), guard.locked():
                guard.maintenance('prepare', OP, 2)
            with guard.installation_intent(OP, 2, 'a' * 64, 'b' * 64, node_lock):
                pass
            attempt(False)
            node_lock.unlink()
            run(['systemctl', 'daemon-reload'])
            attempt(False)
            print('SYSTEMD_PENDING_HELD_INSTALLATION_AND_REENTRY_DENIED=PASS', flush=True)
            print('ENVIRONMENT_OVERRIDE_CANNOT_BYPASS_CANONICAL_JOURNAL=PASS', flush=True)
            (guard.root / 'state.json').unlink()
            attempt(False)
            (guard.root / 'state.json').write_text('{')
            (guard.root / 'state.json').chmod(0o600)
            attempt(False)
            print('SYSTEMD_LOST_OR_CORRUPT_STATE_DENIED=PASS', flush=True)
        finally:
            run(['systemctl', 'stop', unit_name], required=False)
            drop_file.unlink(missing_ok=True)
            if drop_dir.exists():
                drop_dir.rmdir()
            unit.unlink(missing_ok=True)
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'reset-failed', unit_name], required=False)
    print('PANEL_STARTUP_SYSTEMD=PASS; SCOPE=DISPOSABLE_CI_SERVICE', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('PANEL_STARTUP_SYSTEMD=FAILED; TYPE=' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
