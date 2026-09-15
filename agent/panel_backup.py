"""Private operation-bound rollback snapshots; never restore or release access."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat
import sys
import time

import panel_request_guard as journal

PANEL_HOME = Path('/usr/local/x-ui')
PANEL_DB = Path('/etc/x-ui/x-ui.db')
MAX_FILE = 512 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_FILES = 4096


class BackupError(RuntimeError):
    pass


def directory(path, private=False):
    for item in (path, *path.parents):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & (0o077 if private and item == path else 0o022):
            raise BackupError('BACKUP_DIRECTORY_UNSAFE')


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_file(path, limit=MAX_FILE):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                or info.st_mode & 0o022 or info.st_size > limit):
            raise BackupError('BACKUP_FILE_UNSAFE')
        data = bytearray()
        while block := os.read(fd, min(1024 * 1024, limit + 1 - len(data))):
            data.extend(block)
            if len(data) > limit:
                raise BackupError('BACKUP_SIZE_LIMIT')
        after = os.fstat(fd)
        current = path.lstat()
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ) or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise BackupError('BACKUP_SOURCE_CHANGED')
        return bytes(data), stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def write_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def tree(path, target=None):
    directory(path)
    result, total = {}, 0
    def visit(source, relative):
        nonlocal total
        for item in sorted(source.iterdir()):
            name = (relative / item.name).as_posix()
            if len(PurePosixPath(name).parts) > 16 or '\\' in name or len(result) >= MAX_FILES:
                raise BackupError('BACKUP_TREE_LIMIT')
            info = item.lstat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise BackupError('BACKUP_FILE_UNSAFE')
            if stat.S_ISDIR(info.st_mode):
                result[name] = {'kind': 'directory', 'mode': stat.S_IMODE(info.st_mode), 'size': 0}
                if target:
                    (target / name).mkdir(mode=0o700)
                visit(item, PurePosixPath(name))
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > MAX_TOTAL:
                    raise BackupError('BACKUP_SIZE_LIMIT')
                data, mode = read_file(item)
                result[name] = {'kind': 'file', 'mode': mode, 'size': len(data),
                                'sha256': hashlib.sha256(data).hexdigest()}
                if target:
                    write_file(target / name, data)
            else:
                raise BackupError('BACKUP_FILE_UNSAFE')
        if target:
            sync_dir(target / relative)
    visit(path, PurePosixPath())
    return result


def snapshot_database(source, target, timeout=20):
    directory(source.parent)
    # Validate DB and existing SQLite sidecars before letting SQLite open them.
    # Do not read the live DB as a snapshot: committed data may exist only in WAL.
    for path in (source, *(Path(str(source) + suffix) for suffix in ('-wal', '-shm', '-journal'))):
        if path == source or path.exists() or path.is_symlink():
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                    or info.st_mode & 0o022 or info.st_size > MAX_FILE):
                raise BackupError('BACKUP_DATABASE_UNSAFE')
    identity = source.stat()
    write_file(target, b'')
    deadline = time.monotonic() + timeout
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=0.05)) as src:
        page_size = src.execute('PRAGMA page_size').fetchone()[0]
        def progress(status, remaining, total):
            if total * page_size > MAX_FILE or time.monotonic() >= deadline:
                raise BackupError('BACKUP_DATABASE_LIMIT')
        with closing(sqlite3.connect(target, timeout=0.05)) as dst:
            dst.execute('PRAGMA synchronous=FULL')
            src.backup(dst, pages=128, progress=progress, sleep=0.05)
            if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise BackupError('BACKUP_DATABASE_INVALID')
            if dst.execute("SELECT 1 FROM sqlite_schema WHERE type='table' LIMIT 1").fetchone() is None:
                raise BackupError('BACKUP_DATABASE_INVALID')
            # Close a standalone snapshot using SQLite's own journaling protocol.
            if dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                raise BackupError('BACKUP_DATABASE_INVALID')
    fresh = source.stat()
    if (fresh.st_dev, fresh.st_ino) != (identity.st_dev, identity.st_ino):
        raise BackupError('BACKUP_SOURCE_CHANGED')
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    sync_dir(target.parent)


def location(guard, operation_id, generation):
    journal.validate_hold_identity(operation_id, generation)
    return guard.root / ('rollback-' + operation_id + '-' + str(generation))


def validate_manifest(value):
    if (not isinstance(value, dict) or set(value) != {'schema', 'phase', 'intent', 'panel', 'database'}
            or type(value['schema']) is not int or value['schema'] != 1 or value['phase'] != 'SEALED_SNAPSHOT'):
        raise BackupError('BACKUP_MANIFEST_INVALID')
    intent = value['intent']
    if (not isinstance(intent, dict) or set(intent) != {'operation_id', 'generation', 'candidate_sha256', 'nonce', 'panel_home', 'panel_db'}
            or intent['panel_home'] != str(PANEL_HOME) or intent['panel_db'] != str(PANEL_DB)
            or any(not isinstance(intent[k], str) or not re.fullmatch('[a-f0-9]{64}', intent[k])
                   for k in ('candidate_sha256', 'nonce'))):
        raise BackupError('BACKUP_MANIFEST_INVALID')
    journal.validate_hold_identity(intent['operation_id'], intent['generation'])
    if not isinstance(value['panel'], dict) or not 1 <= len(value['panel']) <= MAX_FILES:
        raise BackupError('BACKUP_MANIFEST_INVALID')
    for name, row in value['panel'].items():
        if not isinstance(name, str):
            raise BackupError('BACKUP_MANIFEST_INVALID')
        path = PurePosixPath(name)
        if (not name or name == '.' or path.is_absolute() or str(path) != name or '..' in path.parts or '\\' in name
                or not isinstance(row, dict) or row.get('kind') not in ('file', 'directory')
                or type(row.get('mode')) is not int or row['mode'] & ~0o777 or row['mode'] & 0o022
                or type(row.get('size')) is not int or not 0 <= row['size'] <= MAX_FILE):
            raise BackupError('BACKUP_MANIFEST_INVALID')
        if row['kind'] == 'directory':
            if set(row) != {'kind', 'mode', 'size'} or row['size'] != 0:
                raise BackupError('BACKUP_MANIFEST_INVALID')
        elif set(row) != {'kind', 'mode', 'size', 'sha256'} or not isinstance(row['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', row['sha256']):
            raise BackupError('BACKUP_MANIFEST_INVALID')
    db = value['database']
    if (not isinstance(db, dict) or set(db) != {'size', 'sha256'} or type(db['size']) is not int
            or not 1 <= db['size'] <= MAX_FILE or not isinstance(db['sha256'], str)
            or not re.fullmatch('[a-f0-9]{64}', db['sha256'])):
        raise BackupError('BACKUP_MANIFEST_INVALID')


def verify_locked(guard, operation_id, generation, candidate_sha256, expected_digest=None):
    """Caller retains Node and journal locks. Only verifies; never repairs."""
    root = location(guard, operation_id, generation)
    directory(root, private=True)
    if {path.name for path in root.iterdir()} != {'intent.json', 'manifest.json', 'panel', 'database.sqlite'}:
        raise BackupError('BACKUP_RECONCILIATION_REQUIRED')
    raw, mode = read_file(root / 'manifest.json', 2 * 1024 * 1024)
    if mode != 0o600:
        raise BackupError('BACKUP_FILE_UNSAFE')
    manifest = json.loads(raw, object_pairs_hook=journal.unique_object)
    validate_manifest(manifest)
    expected = {'operation_id': operation_id, 'generation': generation, 'candidate_sha256': candidate_sha256}
    if any(manifest['intent'][key] != value for key, value in expected.items()):
        raise BackupError('BACKUP_BINDING_CHANGED')
    intent_raw, intent_mode = read_file(root / 'intent.json', 4096)
    if intent_mode != 0o600 or json.loads(intent_raw, object_pairs_hook=journal.unique_object) != manifest['intent']:
        raise BackupError('BACKUP_BINDING_CHANGED')
    directory(root / 'panel', private=True)
    stored = {name: {**row, 'mode': 0o700 if row['kind'] == 'directory' else 0o600}
              for name, row in manifest['panel'].items()}
    if tree(root / 'panel') != stored:
        raise BackupError('BACKUP_CONTENT_MISMATCH')
    data, mode = read_file(root / 'database.sqlite')
    if mode != 0o600 or manifest['database'] != {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}:
        raise BackupError('BACKUP_CONTENT_MISMATCH')
    with closing(sqlite3.connect((root / 'database.sqlite').as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise BackupError('BACKUP_DATABASE_INVALID')
    digest = hashlib.sha256(raw).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise BackupError('BACKUP_BINDING_CHANGED')
    return digest


def prepare(guard, operation_id, generation, candidate_sha256, node_lock=None):
    if (sys.platform != 'linux' or os.geteuid() != 0 or guard.root != journal.DEFAULT_ROOT
            or not isinstance(candidate_sha256, str) or not re.fullmatch('[a-f0-9]{64}', candidate_sha256)):
        raise BackupError('BACKUP_SCOPE_INVALID')
    root = location(guard, operation_id, generation)
    with journal.maintenance_node_lock(node_lock or Path('/run/lock/wavemesh-node.lock')), guard.locked():
        directory(guard.root, private=True)
        state = guard.load()
        hold = guard.hold_state(state)
        if (not hold or hold != {'operation_id': operation_id, 'generation': generation, 'phase': 'HELD'}
                or (guard.request_state(state) and guard.request_state(state)['phase'] != 'RESPONSE_ACCEPTED')):
            raise BackupError('BACKUP_ADMISSION_REQUIRED')
        if root.exists() or root.is_symlink():
            installation = state.get('installation')
            if installation and installation['candidate_sha256'] != candidate_sha256:
                raise BackupError('BACKUP_BINDING_CHANGED')
            digest = verify_locked(guard, operation_id, generation, candidate_sha256,
                                   installation['rollback_manifest_sha256'] if installation else None)
            sync_dir(root); sync_dir(guard.root)
            return {'rollback_manifest_sha256': digest, 'reconciliation_required': True}
        if state['schema_version'] != 2:
            raise BackupError('BACKUP_RECONCILIATION_REQUIRED')
        directory(PANEL_HOME)
        root.mkdir(mode=0o700)
        sync_dir(guard.root)
        intent = {'operation_id': operation_id, 'generation': generation, 'candidate_sha256': candidate_sha256,
                  'nonce': secrets.token_hex(32), 'panel_home': str(PANEL_HOME), 'panel_db': str(PANEL_DB)}
        write_file(root / 'intent.json', json.dumps(intent, sort_keys=True).encode())
        sync_dir(root)
        (root / 'panel').mkdir(mode=0o700)
        inventory = tree(PANEL_HOME, root / 'panel')
        snapshot_database(PANEL_DB, root / 'database.sqlite')
        if tree(PANEL_HOME) != inventory:
            raise BackupError('BACKUP_SOURCE_CHANGED')
        data, _ = read_file(root / 'database.sqlite')
        manifest = {'schema': 1, 'phase': 'SEALED_SNAPSHOT', 'intent': intent, 'panel': inventory,
                    'database': {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}
        validate_manifest(manifest)
        write_file(root / 'manifest.pending', json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
        os.rename(root / 'manifest.pending', root / 'manifest.json')
        sync_dir(root); sync_dir(guard.root)
        digest = verify_locked(guard, operation_id, generation, candidate_sha256)
        return {'rollback_manifest_sha256': digest, 'reconciliation_required': False}
