"""Prepare a private, verified candidate. No promotion, execution or replacement."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile

import panel_backup as storage
import panel_request_guard as journal

PANEL_HOME = Path('/usr/local/x-ui')
ARCHIVE = 'x-ui-linux-amd64-wavemesh.tar.gz'
FILES = {'x-ui/x-ui', 'x-ui/x-ui.sh', 'x-ui/x-ui.service.debian', 'x-ui/x-ui.service.rhel',
         'x-ui/x-ui.service.arch', 'x-ui/bin/README.md', 'x-ui/bin/LICENSE',
         'x-ui/bin/mtg-linux-amd64', 'x-ui/bin/xray-linux-amd64',
         'x-ui/bin/geoip.dat', 'x-ui/bin/geosite.dat', 'x-ui/bin/geoip_RU.dat',
         'x-ui/bin/geosite_RU.dat', 'x-ui/bin/geoip_IR.dat', 'x-ui/bin/geosite_IR.dat'}
DIRECTORIES = {'x-ui', 'x-ui/bin'}
EXECUTABLES = {'x-ui/x-ui', 'x-ui/bin/mtg-linux-amd64', 'x-ui/bin/xray-linux-amd64'}
MANIFEST_KEYS = {'schema', 'status', 'platform', 'builder_commit', 'upstream', 'version',
                 'runtime_release_sha256', 'source_sha256', 'archive_sha256', 'members',
                 'frontend', 'toolchain', 'workflow_run', 'workflow_attempt'}


class CandidateError(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def valid_hash(value, length=64):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{' + str(length) + '}', value)


def manifest(raw, expected_digest, head):
    # The caller must obtain this digest from a separate trusted approval path.
    # Reading a digest from the downloaded manifest is not an approval.
    if not valid_hash(expected_digest) or not valid_hash(head, 40) or digest(raw) != expected_digest:
        raise CandidateError('CANDIDATE_TRUST_BINDING_INVALID')
    value = json.loads(raw, object_pairs_hook=journal.unique_object)
    if (not isinstance(value, dict) or set(value) != MANIFEST_KEYS
            or type(value['schema']) is not int or value['schema'] != 1
            or value['status'] != 'CI_CANDIDATE_NOT_DEPLOYED' or value['platform'] != 'linux-amd64'
            or value['builder_commit'] != head or value['version'] != '3.4.2-wavemesh.' + head[:12]
            or any(not valid_hash(value[key]) for key in ('archive_sha256', 'source_sha256', 'runtime_release_sha256'))):
        raise CandidateError('CANDIDATE_MANIFEST_INVALID')
    members = value['members']
    if not isinstance(members, dict) or set(members) != FILES | DIRECTORIES:
        raise CandidateError('CANDIDATE_INVENTORY_INVALID')
    total = 0
    for name, row in members.items():
        is_dir = name in DIRECTORIES
        keys = {'kind', 'mode', 'size'} | (set() if is_dir else {'sha256'})
        mode = 0o755 if is_dir or name in EXECUTABLES else 0o644
        if (not isinstance(row, dict) or set(row) != keys
                or row['kind'] != ('directory' if is_dir else 'file')
                or type(row['mode']) is not int or row['mode'] != mode
                or type(row['size']) is not int or not 0 <= row['size'] <= storage.MAX_FILE
                or (is_dir and row['size'] != 0) or (not is_dir and not valid_hash(row['sha256']))):
            raise CandidateError('CANDIDATE_INVENTORY_INVALID')
        total += row['size']
    if total > storage.MAX_TOTAL:
        raise CandidateError('CANDIDATE_SIZE_LIMIT')
    return value


def extract(archive, members, target):
    """Read fixed-size tar headers before payload; never delegate link/PAX handling.

    The reviewed packager produces plain regular files/directories. Reject GNU
    extensions, sparse/PAX records and concatenated hidden members, rather than
    asking a generic extractor to interpret attacker-controlled metadata.
    """
    seen = set()
    for name in sorted(DIRECTORIES):
        (target / name).mkdir(mode=0o700)
    with gzip.GzipFile(fileobj=io.BytesIO(archive), mode='rb') as stream:
        while True:
            header = stream.read(512)
            if len(header) != 512:
                raise CandidateError('CANDIDATE_ARCHIVE_INVALID')
            if header == bytes(512):
                tail = stream.read(10241)
                if len(tail) < 512 or len(tail) > 10240 or any(tail):
                    raise CandidateError('CANDIDATE_ARCHIVE_INVALID')
                break
            member = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
            name = member.name
            if (name not in members or name in seen or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)
                    or member.linkname or member.uid != 0 or member.gid != 0):
                raise CandidateError('CANDIDATE_ARCHIVE_INVALID')
            seen.add(name)
            row = members[name]
            if (member.size != row['size'] or member.mode != row['mode']
                    or member.isdir() != (row['kind'] == 'directory')):
                raise CandidateError('CANDIDATE_ARCHIVE_INVALID')
            if member.isfile():
                data = stream.read(member.size)
                if len(data) != member.size or digest(data) != row['sha256']:
                    raise CandidateError('CANDIDATE_CONTENT_MISMATCH')
                storage.write_file(target / name, data)
            padding = (-member.size) % 512
            if stream.read(padding) != bytes(padding):
                raise CandidateError('CANDIDATE_ARCHIVE_INVALID')
    if seen != set(members):
        raise CandidateError('CANDIDATE_INVENTORY_INVALID')
    for name in sorted(DIRECTORIES, reverse=True):
        storage.sync_dir(target / name)
    storage.sync_dir(target)


def location(operation_id, generation):
    journal.validate_hold_identity(operation_id, generation)
    return PANEL_HOME.parent / ('.wavemesh-candidate-' + operation_id + '-' + str(generation))


def identity(guard, operation_id, generation, manifest_sha256, head):
    return {'operation_id': operation_id, 'generation': generation, 'manifest_sha256': manifest_sha256,
            'builder_commit': head, 'panel_home': str(PANEL_HOME), 'journal_root': str(guard.root)}


def verify_locked(guard, operation_id, generation, manifest_sha256, head):
    """Caller retains both locks; all returned paths remain private inert copies."""
    root = location(operation_id, generation)
    storage.directory(root, private=True)
    if {item.name for item in root.iterdir()} != {'intent.json', 'ready.json', 'manifest.json', 'source.tar.gz', ARCHIVE, 'payload'}:
        raise CandidateError('CANDIDATE_RECONCILIATION_REQUIRED')
    expected = identity(guard, operation_id, generation, manifest_sha256, head)
    for name, value in (('intent.json', expected), ('ready.json', {'schema': 1, 'phase': 'PREPARED', 'intent': expected})):
        raw, mode = storage.read_file(root / name, 4096)
        actual = json.loads(raw, object_pairs_hook=journal.unique_object)
        # JSON identity is type-sensitive: True/1 and 1.0/1 are not aliases.
        if mode != 0o600 or json.dumps(actual, sort_keys=True) != json.dumps(value, sort_keys=True):
            raise CandidateError('CANDIDATE_BINDING_CHANGED')
    raw, mode = storage.read_file(root / 'manifest.json', 1024 * 1024)
    if mode != 0o600:
        raise CandidateError('CANDIDATE_STORAGE_UNSAFE')
    value = manifest(raw, manifest_sha256, head)
    for name, key in ((ARCHIVE, 'archive_sha256'), ('source.tar.gz', 'source_sha256')):
        data, mode = storage.read_file(root / name)
        if mode != 0o600 or digest(data) != value[key]:
            raise CandidateError('CANDIDATE_CONTENT_MISMATCH')
    storage.directory(root / 'payload', private=True)
    expected_tree = {name: {**row, 'mode': 0o700 if name in DIRECTORIES else 0o600}
                     for name, row in value['members'].items()}
    if storage.tree(root / 'payload') != expected_tree:
        raise CandidateError('CANDIDATE_CONTENT_MISMATCH')
    installation = (guard.load() or {}).get('installation')
    if installation and installation['candidate_sha256'] != value['archive_sha256']:
        raise CandidateError('CANDIDATE_BINDING_CHANGED')
    return {'candidate_sha256': value['archive_sha256'], 'manifest_sha256': manifest_sha256,
            'panel_sha256': value['members']['x-ui/x-ui']['sha256'], 'prepared_home': root / 'payload/x-ui'}


def prepare(guard, operation_id, generation, bundle, manifest_sha256, head, node_lock=None):
    if (sys.platform != 'linux' or os.geteuid() != 0 or guard.root != journal.DEFAULT_ROOT
            or not valid_hash(manifest_sha256) or not valid_hash(head, 40)):
        raise CandidateError('CANDIDATE_SCOPE_INVALID')
    root = location(operation_id, generation)
    with journal.maintenance_node_lock(node_lock or Path('/run/lock/wavemesh-node.lock')), guard.locked():
        storage.directory(guard.root, private=True)
        state = guard.load()
        if (guard.hold_state(state) != {'operation_id': operation_id, 'generation': generation, 'phase': 'HELD'}
                or (guard.request_state(state) and guard.request_state(state)['phase'] != 'RESPONSE_ACCEPTED')):
            raise CandidateError('CANDIDATE_ADMISSION_REQUIRED')
        storage.directory(PANEL_HOME)
        if PANEL_HOME.stat().st_dev != PANEL_HOME.parent.stat().st_dev:
            raise CandidateError('CANDIDATE_FILESYSTEM_UNSUPPORTED')
        if root.exists() or root.is_symlink():
            result = verify_locked(guard, operation_id, generation, manifest_sha256, head)
            storage.sync_dir(root); storage.sync_dir(root.parent)
            return {**result, 'reconciliation_required': True}
        if state['schema_version'] != 2:
            raise CandidateError('CANDIDATE_RECONCILIATION_REQUIRED')
        storage.directory(bundle)
        raw, _ = storage.read_file(bundle / 'manifest.json', 1024 * 1024)
        value = manifest(raw, manifest_sha256, head)
        # Pin the exact immutable bytes before extraction. Nothing is executed.
        archive, _ = storage.read_file(bundle / ARCHIVE)
        source, _ = storage.read_file(bundle / 'source.tar.gz')
        if digest(archive) != value['archive_sha256'] or digest(source) != value['source_sha256']:
            raise CandidateError('CANDIDATE_CONTENT_MISMATCH')
        root.mkdir(mode=0o700)
        storage.sync_dir(root.parent)
        intent = identity(guard, operation_id, generation, manifest_sha256, head)
        storage.write_file(root / 'intent.json', json.dumps(intent, sort_keys=True).encode())
        storage.sync_dir(root)
        storage.write_file(root / 'manifest.json', raw)
        storage.write_file(root / ARCHIVE, archive)
        storage.write_file(root / 'source.tar.gz', source)
        (root / 'payload').mkdir(mode=0o700)
        extract(archive, value['members'], root / 'payload')
        ready = {'schema': 1, 'phase': 'PREPARED', 'intent': intent}
        storage.write_file(root / 'ready.pending', json.dumps(ready, sort_keys=True).encode())
        os.rename(root / 'ready.pending', root / 'ready.json')
        storage.sync_dir(root); storage.sync_dir(root.parent)
        return {**verify_locked(guard, operation_id, generation, manifest_sha256, head), 'reconciliation_required': False}
