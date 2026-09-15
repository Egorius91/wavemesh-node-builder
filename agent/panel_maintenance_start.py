"""Internal replacement-to-maintenance admission. No runtime release or CLI.

The caller supplies an independently approved manifest digest and builder head.
Only the pinned backend contract below is supported; matching environment text
alone never establishes that an arbitrary binary implements maintenance.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import select

import panel_backup as storage
import panel_candidate as candidate
import panel_replace as replacement
import panel_request_guard as journal
from panel_start import PanelStart, StartError, property_data

MAINTENANCE_UPSTREAM = {
    'upstream_repository': 'MHSanaei/3x-ui', 'upstream_tag': 'v3.4.2',
    'upstream_commit': 'f3a57d4c57fbcae94414138de42b7ef11dc513c8',
    'patch_sha256': '9b87aefa243a788a991921816511fa52bc23e32b693381776158b317f1facb8d',
}
MODE = 'WAVEMESH_PANEL_MODE=maintenance'
ENVIRONMENT_KEYS = {'PATH', 'HOME', 'LANG', 'XUI_DB_FOLDER', 'XUI_BIN_FOLDER',
                    'XUI_LOG_FOLDER', 'XUI_LOG_LEVEL', 'WAVEMESH_PANEL_MODE'}


def validate_environment(entries):
    if not isinstance(entries, list) or any(not isinstance(row, str) for row in entries):
        raise StartError('MAINTENANCE_ENVIRONMENT_UNSUPPORTED')
    values = {}
    for row in entries:
        key, separator, value = row.partition('=')
        if not separator or key not in ENVIRONMENT_KEYS or key in values or '\x00' in value:
            raise StartError('MAINTENANCE_ENVIRONMENT_UNSUPPORTED')
        values[key] = value
    if entries.count(MODE) != 1:
        raise StartError('MAINTENANCE_MODE_REQUIRED')
    return sorted(entries)


class PanelMaintenanceStart(PanelStart):
    initial_version = 6
    active_version = 7

    def __init__(self, manifest_sha256, head):
        if not candidate.valid_hash(manifest_sha256) or not candidate.valid_hash(head, 40):
            raise StartError('MAINTENANCE_TRUST_REQUIRED')
        self.manifest_sha256, self.head = manifest_sha256, head

    def environment(self):
        # EnvironmentFile overrides Environment; UnsetEnvironment applies last.
        # Reject alternate sources instead of guessing effective precedence.
        for key, signature in (('EnvironmentFiles', 'a(sb)'), ('PassEnvironment', 'as'),
                               ('UnsetEnvironment', 'as')):
            if property_data(self.property(key), signature) != []:
                raise StartError('MAINTENANCE_ENVIRONMENT_UNSUPPORTED')
        return validate_environment(property_data(self.property('Environment'), 'as'))

    def start_contract(self, observed, helper_sha256, executable_sha256):
        base = super().start_contract(observed, helper_sha256, executable_sha256)
        return hashlib.sha256(json.dumps([base, self.environment(), self.manifest_sha256,
                                         self.head, MAINTENANCE_UPSTREAM], sort_keys=True).encode()).hexdigest()

    def verify_source(self, guard, state, executable_sha256):
        row = state['replacement']
        if row['phase'] != 'REPLACED' or row['manifest_sha256'] != self.manifest_sha256:
            raise StartError('MAINTENANCE_REPLACEMENT_REQUIRED')
        hold, install = state['maintenance'], state['installation']
        operation, generation = hold['operation_id'], hold['generation']
        prepared = candidate.verify_locked(guard, operation, generation, self.manifest_sha256, self.head)
        if prepared['panel_sha256'] != executable_sha256:
            raise StartError('MAINTENANCE_EXECUTABLE_MISMATCH')
        raw, _ = storage.read_file(candidate.location(operation, generation) / 'manifest.json', 1024 * 1024)
        manifest = candidate.manifest(raw, self.manifest_sha256, self.head)
        if manifest['upstream'] != MAINTENANCE_UPSTREAM:
            raise StartError('MAINTENANCE_BACKEND_UNSUPPORTED')
        storage.verify_locked(guard, operation, generation, install['candidate_sha256'],
                              install['rollback_manifest_sha256'])
        raw, _ = storage.read_file(storage.location(guard, operation, generation) / 'manifest.json', 2 * 1024 * 1024)
        original = json.loads(raw, object_pairs_hook=journal.unique_object)['panel']
        expected = {name.removeprefix('x-ui/'): data for name, data in manifest['members'].items() if name != 'x-ui'}
        root = replacement.location(operation, generation)
        storage.directory(root, private=True)
        if {path.name for path in root.iterdir()} != {'receipt.json', 'slot'}:
            raise StartError('MAINTENANCE_RECEIPT_INVALID')
        raw, mode = storage.read_file(root / 'receipt.json', 4096)
        if mode != 0o600 or hashlib.sha256(raw).hexdigest() != row['receipt_sha256']:
            raise StartError('MAINTENANCE_RECEIPT_INVALID')
        receipt = json.loads(raw, object_pairs_hook=journal.unique_object)
        identity = [operation, generation, install['candidate_sha256'], install['rollback_manifest_sha256'],
                    self.manifest_sha256, self.head, state['stop']['boot_id'], state['stop']['contract_sha256']]
        replacement.validate_receipt(receipt, identity, original, expected)
        if replacement.PanelReplacement().position(root, receipt) != 'candidate':
            raise StartError('MAINTENANCE_REPLACEMENT_REQUIRED')

    def admit(self, guard, connection, state, helper, executable, expected):
        self.verify_source(guard, state, executable)
        return super().admit(guard, connection, state, helper, executable, expected)

    def verify_running(self, state, helper, executable):
        observed = self.observe()
        super().verify_running(state, helper, executable)
        if self.observe() != observed:
            raise StartError('MAINTENANCE_PROCESS_CHANGED')
        pid = int(observed['MainPID'])
        fd = os.pidfd_open(pid)
        try:
            with open('/proc/' + str(pid) + '/environ', 'rb') as source:
                raw = source.read(65537)
            if len(raw) > 65536 or not raw.endswith(b'\0'):
                raise StartError('MAINTENANCE_PROCESS_ENVIRONMENT_INVALID')
            entries = raw[:-1].split(b'\0')
            modes = [row for row in entries if row.startswith(b'WAVEMESH_PANEL_MODE=')]
            if modes != [MODE.encode()]:
                raise StartError('MAINTENANCE_PROCESS_MODE_INVALID')
            poll = select.poll(); poll.register(fd, select.POLLIN)
            if poll.poll(0) or self.observe() != observed:
                raise StartError('MAINTENANCE_PROCESS_CHANGED')
        finally:
            os.close(fd)

    @contextmanager
    def started(self, *args, **kwargs):
        with super().started(*args, **kwargs) as result:
            yield {**result, 'activation': 'MAINTENANCE_BOUND_INVOCATION',
                   'runtime_activation': 'DENIED', 'authority_reconciliation': 'REQUIRED'}
