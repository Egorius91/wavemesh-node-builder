#!/usr/bin/env python3
"""Finite owner-only repair core. Production entrypoint is inert until admission binds.

This module deliberately does not import a fleet reader, accept quiescence JSON,
inspect private selectors, or reopen a command gate. Native effects are future
grant actions. Portable tests inject mock objects, never native acceptance proof.
"""
from __future__ import annotations

import array
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

TARGETS = ('wm-stage-entry', 'wm-exit-2', 'wm-exit-de-frankfurt-2')
HOME = '/usr/local/x-ui'
DIRECTORIES = (HOME, HOME + '/bin')
MEMBERS = DIRECTORIES + tuple(HOME + '/' + p for p in (
    'x-ui', 'x-ui.sh', 'x-ui.service.debian', 'x-ui.service.rhel', 'x-ui.service.arch',
    'bin/geoip.dat', 'bin/geoip_RU.dat', 'bin/geoip_IR.dat', 'bin/geosite.dat',
    'bin/geosite_RU.dat', 'bin/geosite_IR.dat', 'bin/README.md', 'bin/LICENSE',
    'bin/mtg-linux-amd64', 'bin/xray-linux-amd64'))
UNITS = ('wavemesh-node-agent.service', 'x-ui.service')
LOCK = '/run/lock/wavemesh-node.lock'
JOURNAL_ROOT = '/var/lib/wavemesh-maintenance/xui-owner-repair'
OPERATOR_PATH = '/usr/local/lib/wavemesh-operations/xui_owner_repair.py'
UNIT_PROPS = ('Id', 'LoadState', 'ActiveState', 'SubState', 'MainPID', 'ControlPID', 'ControlGroup',
              'Job', 'KillMode', 'SendSIGKILL', 'TimeoutStopUSec', 'TimeoutStartUSec',
              'FileDescriptorStoreMax', 'NFileDescriptorStore', 'FileDescriptorStorePreserve',
              'TriggeredBy', 'Triggers', 'OnFailure', 'OnSuccess', 'NeedDaemonReload',
              'Restart', 'RefuseManualStart', 'RefuseManualStop', 'PartOf', 'BindsTo',
              'BoundBy', 'ConsistsOf', 'Upholds', 'UpheldBy', 'After', 'Before', 'Requires',
              'Wants', 'RequiredBy', 'WantedBy', 'FragmentPath', 'DropInPaths')
ACTIVATION_PROPS = ('PartOf', 'BindsTo', 'BoundBy', 'ConsistsOf', 'Upholds', 'UpheldBy',
                    'After', 'Before', 'Requires', 'Wants', 'RequiredBy', 'WantedBy',
                    'FragmentPath', 'DropInPaths', 'Restart', 'TimeoutStopUSec', 'TimeoutStartUSec')
MAX_PUBLIC_BYTES = 128 * 1024 * 1024
MAX_LEDGER_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 1024


class Stop(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise Stop(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def validate_manifest(document):
    require(type(document) is dict and set(document) == {'schema', 'target', 'operation', 'objects'}, 'MANIFEST_SCHEMA')
    require(type(document['schema']) is int and document['schema'] == 1, 'MANIFEST_SCHEMA')
    require(document['target'] in TARGETS, 'TARGET_NOT_ALLOWED')
    require(type(document['operation']) is str and re.fullmatch('[a-f0-9]{32}', document['operation']), 'OPERATION_ID')
    objects = document['objects']
    require(type(objects) is list and len(objects) == 17, 'EXACT_OBJECT_SET')
    require(all(type(p) is dict for p in objects), 'MANIFEST_SCHEMA')
    require([p.get('path') for p in objects] == list(MEMBERS), 'EXACT_OBJECT_SET')
    required = {'path', 'kind', 'dev', 'ino', 'mode', 'size', 'mtime_ns', 'flags', 'nlink', 'sha256'}
    identities = set()
    for pin in objects:
        require(set(pin) == required, 'MANIFEST_SCHEMA')
        directory = pin['path'] in DIRECTORIES
        require(pin['kind'] == ('directory' if directory else 'regular'), 'OBJECT_TYPE')
        require(all(type(pin[k]) is int and pin[k] >= 0 for k in ('dev', 'ino', 'mode', 'size', 'mtime_ns', 'flags', 'nlink')), 'MANIFEST_NUMERIC')
        require(pin['ino'] > 0 and pin['nlink'] >= 1, 'OBJECT_IDENTITY')
        require(0 < pin['mode'] <= 0o777 and not pin['mode'] & 0o022, 'UNSAFE_MODE')
        require(not pin['flags'] & (0x10 | 0x20), 'UNSAFE_FLAGS')
        require(pin['size'] <= MAX_PUBLIC_BYTES, 'PUBLIC_FILE_SIZE')
        require((directory and pin['sha256'] is None) or
                (not directory and type(pin['sha256']) is str and re.fullmatch('[a-f0-9]{64}', pin['sha256'])), 'PUBLIC_HASH')
        require(directory or pin['nlink'] == 1, 'HARDLINK')
        identity = (pin['dev'], pin['ino'])
        require(identity not in identities, 'DUPLICATE_INODE')
        identities.add(identity)
    require(len({p['dev'] for p in objects}) == 1, 'CROSS_DEVICE_OBJECT_SET')
    require(sum(p['size'] for p in objects if p['kind'] == 'regular') <= 512 * 1024 * 1024, 'TOTAL_PUBLIC_SIZE')
    return document


def encode_record(seq, previous, data):
    payload = {'seq': seq, 'previous': previous, 'data': data}
    return canonical({**payload, 'checksum': digest(canonical(payload))}) + b'\n'


def decode_records(raw):
    require(len(raw) <= MAX_LEDGER_BYTES and (not raw or raw.endswith(b'\n')), 'LEDGER_TORN_OR_OVERSIZE')
    records, previous = [], '0' * 64
    for seq, line in enumerate(raw.splitlines(keepends=True)):
        require(seq < MAX_RECORDS, 'LEDGER_LIMIT')
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            raise Stop('LEDGER_INVALID') from None
        require(type(record) is dict and set(record) == {'seq', 'previous', 'data', 'checksum'} and
                record['seq'] == seq and record['previous'] == previous and type(record['data']) is dict,
                'LEDGER_CHAIN')
        require(encode_record(seq, previous, record['data']) == line, 'LEDGER_NONCANONICAL')
        previous = digest(line)
        records.append(record['data'])
    return records


def last_object_event(journal, pin):
    found = None
    for record in journal.records:
        if record.get('path') == pin['path']:
            require(record.get('pin') == pin, 'LEDGER_OBJECT_BINDING')
            found = record
    return found


def reconcile_objects(document, objects, journal, assert_closed):
    """Explicit reconciliation is read-only for release objects. Preserve root subset."""
    validate_manifest(document)
    assert_closed()
    result = []
    for pin in document['objects']:
        assert_closed()
        last = last_object_event(journal, pin)
        owner = objects.check(pin, root_allowed=last is not None)
        require(owner != (0, 0) or last is not None, 'UNATTRIBUTED_ROOT_OBJECT')
        if last is not None:
            journal.append('RECONCILED', path=pin['path'], pin=pin, owner=list(owner))
        result.append({'path': pin['path'], 'owner': list(owner)})
    return result


def apply_owner_only(document, objects, journal, assert_closed):
    validate_manifest(document)
    assert_closed()
    for pin in document['objects']:
        assert_closed()
        last = last_object_event(journal, pin)
        require(last is None or last['event'] in {'APPLIED', 'RECONCILED'}, 'EXPLICIT_RECONCILIATION_REQUIRED')
        owner = objects.check(pin, root_allowed=last is not None)
        if owner == (0, 0):
            require(last is not None and (last['event'] == 'APPLIED' or last.get('owner') == [0, 0]), 'UNATTRIBUTED_ROOT_OBJECT')
            continue
        require(owner == (1001, 1001), 'UNEXPECTED_OWNER')
        assert_closed()
        journal.append('INTENT', path=pin['path'], pin=pin)
        try:
            objects.chown(pin)
            require(objects.check(pin, root_allowed=True) == (0, 0), 'OWNER_READBACK')
            journal.append('APPLIED', path=pin['path'], pin=pin)
        except Exception:
            # INTENT is durable before fchown; never retry an uncertain syscall.
            raise Stop('OWNER_OUTCOME_UNKNOWN') from None


def verify_objects(document, objects, assert_closed):
    """Call with a freshly opened independent NativeObjects instance after apply."""
    validate_manifest(document)
    assert_closed()
    for pin in document['objects']:
        assert_closed()
        require(objects.check(pin, root_allowed=True) == (0, 0), 'OWNER_SET_INCOMPLETE')
    assert_closed()
    return len(document['objects'])


def metadata(info):
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
            info.st_uid, info.st_gid, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def safe_root_directory(info):
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_gid == 0 and
            not info.st_mode & 0o022, 'UNSAFE_ANCESTOR')


def safe_lock_parent(info):
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == info.st_gid == 0 and
            (not info.st_mode & 0o022 or stat.S_IMODE(info.st_mode) == 0o1777), 'UNSAFE_LOCK_PARENT')


def open_absolute_directory(path, *, create=False, sticky_lock=False):
    """Anchor every path component; never traverse a symlink or writable ancestor."""
    require(path.startswith('/') and '..' not in path.split('/'), 'ABSOLUTE_PATH')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        safe_root_directory(os.fstat(fd))
        walked = ''
        for component in path.strip('/').split('/') if path != '/' else ():
            walked += '/' + component
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                info = os.fstat(nxt)
                if sticky_lock and walked == '/run/lock':
                    safe_lock_parent(info)
                else:
                    safe_root_directory(info)
                require(metadata(os.stat(component, dir_fd=fd, follow_symlinks=False)) == metadata(os.fstat(nxt)), 'ANCESTOR_CHANGED')
            except Exception:
                os.close(nxt)
                raise
            os.close(fd)
            fd = nxt
        return fd
    except Exception:
        os.close(fd)
        raise


class NativeObjects:
    """Only the seventeen fixed public paths; no directory listing or private read."""
    def __init__(self, document):
        validate_manifest(document)
        self.pins = {p['path']: p for p in document['objects']}
        self.parent = open_absolute_directory('/usr/local')
        self.fds = {}
        try:
            for pin in document['objects']:
                relative = pin['path'][len(HOME):].lstrip('/')
                parent, name = (self.parent, 'x-ui') if not relative else (self.fds[HOME], relative)
                if relative.startswith('bin/'):
                    parent, name = self.fds[HOME+'/bin'], relative[4:]
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                flags |= os.O_DIRECTORY if pin['kind'] == 'directory' else os.O_NOATIME
                fd = os.open(name, flags, dir_fd=parent)
                self.fds[pin['path']] = fd
                # Compare descriptor to current anchored name, not just pathname text.
                self.check(pin, root_allowed=True)
        except Exception:
            self.close()
            raise

    @classmethod
    def capture(cls, target, operation, expected_identities, assert_closed):
        """Future fresh read-only seventeen-object admission; never mutates owners."""
        require(target in TARGETS and type(operation) is str and re.fullmatch('[a-f0-9]{32}', operation), 'CAPTURE_IDENTITY')
        require(type(expected_identities) is dict and set(expected_identities) == set(MEMBERS) and
                all(type(v) in {tuple, list} and len(v) == 2 and all(type(n) is int and n >= 0 for n in v)
                    for v in expected_identities.values()), 'CAPTURE_EXPECTED_IDENTITIES')
        assert_closed()
        self = cls.__new__(cls)
        self.parent = open_absolute_directory('/usr/local')
        self.fds, self.pins = {}, {}
        try:
            pins = []
            for path in MEMBERS:
                assert_closed()
                parent, name = (self.parent, 'x-ui') if path == HOME else (self.fds[HOME], path[len(HOME)+1:])
                if path.startswith(HOME+'/bin/'):
                    parent, name = self.fds[HOME+'/bin'], path[len(HOME+'/bin/'):]
                directory = path in DIRECTORIES
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                flags |= os.O_DIRECTORY if directory else os.O_NOATIME
                fd = os.open(name, flags, dir_fd=parent)
                self.fds[path] = fd
                before = os.fstat(fd)
                require((before.st_dev, before.st_ino) == tuple(expected_identities[path]), 'CAPTURE_OBJECT_REPLACED')
                require((before.st_uid, before.st_gid) == (1001, 1001), 'CAPTURE_INITIAL_OWNER')
                require(not stat.S_IMODE(before.st_mode) & 0o6022, 'UNSAFE_MODE')
                import fcntl
                inode_flags = array.array('I', [0])
                fcntl.ioctl(fd, 0x80086601, inode_flags, True)
                pin = {'path': path, 'kind': 'directory' if directory else 'regular',
                       'dev': before.st_dev, 'ino': before.st_ino, 'mode': stat.S_IMODE(before.st_mode),
                       'size': before.st_size, 'mtime_ns': before.st_mtime_ns,
                       'flags': inode_flags[0], 'nlink': before.st_nlink, 'sha256': None}
                if not directory:
                    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
                            before.st_size <= MAX_PUBLIC_BYTES and not os.listxattr(fd), 'CAPTURE_PUBLIC_FILE')
                    value, size = hashlib.sha256(), 0
                    while True:
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        require(size <= MAX_PUBLIC_BYTES, 'PUBLIC_FILE_SIZE')
                        value.update(chunk)
                    pin['sha256'] = value.hexdigest()
                    require(size == before.st_size, 'OBJECT_CHANGED_DURING_READ')
                require(metadata(before) == metadata(os.fstat(fd)), 'OBJECT_CHANGED_DURING_READ')
                self.pins[path] = pin
                self.check(pin)
                pins.append(pin)
            document = validate_manifest({'schema': 1, 'target': target, 'operation': operation, 'objects': pins})
            # Full second finite pass closes changes between individual reads.
            for pin in pins:
                assert_closed()
                self.check(pin)
            return document
        finally:
            self.close()

    def close(self):
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()
        os.close(self.parent)

    def named_stat(self, path):
        if path == HOME:
            return os.stat('x-ui', dir_fd=self.parent, follow_symlinks=False)
        relative = path[len(HOME)+1:]
        parent, name = self.fds[HOME], relative
        if relative.startswith('bin/'):
            parent, name = self.fds[HOME+'/bin'], relative[4:]
        return os.stat(name, dir_fd=parent, follow_symlinks=False)

    def check(self, pin, *, root_allowed=False):
        import fcntl
        # Re-open the trusted prefix and compare HOME/bin named anchors on every
        # member. A leaf FD in a renamed old directory is not current-path proof.
        fresh = open_absolute_directory('/usr/local')
        try:
            require((os.fstat(fresh).st_dev, os.fstat(fresh).st_ino) ==
                    (os.fstat(self.parent).st_dev, os.fstat(self.parent).st_ino), 'ANCESTOR_PATH_CHANGED')
        finally:
            os.close(fresh)
        for path in DIRECTORIES:
            if path not in self.fds:
                continue
            value, expected = os.fstat(self.fds[path]), self.pins[path]
            require(metadata(value) == metadata(self.named_stat(path)), 'DIRECTORY_PATH_CHANGED')
            require((value.st_dev, value.st_ino, stat.S_IMODE(value.st_mode), value.st_nlink) ==
                    tuple(expected[k] for k in ('dev', 'ino', 'mode', 'nlink')) and
                    (value.st_uid, value.st_gid) in {(1001, 1001), (0, 0)}, 'DIRECTORY_ANCHOR_DRIFT')
        fd = self.fds[pin['path']]
        before = os.fstat(fd)
        require(metadata(before) == metadata(self.named_stat(pin['path'])), 'OBJECT_PATH_CHANGED')
        directory = pin['kind'] == 'directory'
        require((stat.S_ISDIR(before.st_mode) if directory else stat.S_ISREG(before.st_mode)), 'OBJECT_TYPE')
        require((before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode), before.st_size, before.st_mtime_ns, before.st_nlink) ==
                tuple(pin[k] for k in ('dev', 'ino', 'mode', 'size', 'mtime_ns', 'nlink')), 'OBJECT_METADATA_DRIFT')
        allowed = {(1001, 1001), (0, 0)} if root_allowed else {(1001, 1001)}
        require((before.st_uid, before.st_gid) in allowed, 'UNEXPECTED_OWNER')
        require(not os.listxattr(fd), 'UNEXPECTED_XATTR')
        flags = array.array('I', [0])
        fcntl.ioctl(fd, 0x80086601, flags, True)
        require(flags[0] == pin['flags'] and not flags[0] & 0x30, 'OBJECT_FLAGS_DRIFT')
        if not directory:
            os.lseek(fd, 0, os.SEEK_SET)
            value, count = hashlib.sha256(), 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                count += len(chunk)
                require(count <= MAX_PUBLIC_BYTES, 'PUBLIC_FILE_SIZE')
                value.update(chunk)
            require(count == pin['size'] and value.hexdigest() == pin['sha256'], 'PUBLIC_BYTES_CHANGED')
        require(metadata(before) == metadata(os.fstat(fd)) == metadata(self.named_stat(pin['path'])), 'OBJECT_CHANGED_DURING_READ')
        return before.st_uid, before.st_gid

    def chown(self, pin):
        require(self.check(pin) == (1001, 1001), 'OWNER_PRECONDITION')
        os.fchown(self.fds[pin['path']], 0, 0)


class Journal:
    """Protected append-only intent/result ledger, fsynced before every effect."""
    def __init__(self, directory, binding):
        require(type(binding) is dict and type(binding.get('operation')) is str and
                re.fullmatch('[a-f0-9]{32}', binding['operation']) and
                directory == JOURNAL_ROOT + '/' + binding['operation'], 'LEDGER_PATH_BINDING')
        require(set(binding) == {'target', 'operation', 'manifest_sha256', 'operator_sha256', 'namespace'} and
                binding['target'] in TARGETS and all(type(binding[k]) is str and
                re.fullmatch('[a-f0-9]{64}', binding[k]) for k in ('manifest_sha256', 'operator_sha256')),
                'LEDGER_SOURCE_BINDING')
        self.directory = open_absolute_directory(directory, create=True)
        self.binding = binding
        self.fd = None
        try:
            self._open(binding)
        except Exception:
            self.close()
            raise

    def _open(self, binding):
        name = 'ledger.jsonl'
        created = False
        try:
            self.fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL |
                              os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.directory)
            created = True
            os.fsync(self.directory)
        except FileExistsError:
            self.fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.directory)
        info = os.fstat(self.fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 0 and
                stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, 'UNSAFE_LEDGER')
        self.identity = info.st_dev, info.st_ino
        raw = os.read(self.fd, MAX_LEDGER_BYTES + 1)
        require(created or raw, 'EXISTING_EMPTY_LEDGER_NOT_ADOPTED')
        self.records = decode_records(raw)
        self.previous = digest(raw.splitlines(keepends=True)[-1]) if raw else '0' * 64
        if not self.records:
            self.append('BEGIN', binding=binding)
        require(self.records[0] == {'event': 'BEGIN', 'binding': binding}, 'LEDGER_OPERATION_BINDING')

    def append(self, event, **fields):
        info = os.stat('ledger.jsonl', dir_fd=self.directory, follow_symlinks=False)
        require((info.st_dev, info.st_ino) == self.identity and info.st_nlink == 1 and
                info.st_uid == info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o600,
                'LEDGER_REPLACED')
        data = {'event': event, **fields}
        encoded = encode_record(len(self.records), self.previous, data)
        raw = os.pread(self.fd, MAX_LEDGER_BYTES + 1, 0)
        require(decode_records(raw) == self.records, 'LEDGER_CONCURRENT_CHANGE')
        require(len(self.records) < MAX_RECORDS and os.fstat(self.fd).st_size + len(encoded) <= MAX_LEDGER_BYTES, 'LEDGER_LIMIT')
        require(os.write(self.fd, encoded) == len(encoded), 'LEDGER_SHORT_WRITE')
        os.fsync(self.fd)
        self.previous = digest(encoded)
        self.records.append(data)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.directory is not None:
            os.close(self.directory)
            self.directory = None


@contextlib.contextmanager
def existing_mutation_lock():
    """Reuse Agent/CLI's inode without O_CREAT, truncation, unlink or sidecar."""
    import fcntl
    parent = open_absolute_directory('/run/lock', sticky_lock=True)
    fd = None
    try:
        fd = os.open('wavemesh-node.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 0 and
                stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, 'UNSAFE_EXISTING_LOCK')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        named = os.stat('wavemesh-node.lock', dir_fd=parent, follow_symlinks=False)
        require(metadata(info) == metadata(named), 'LOCK_PATH_CHANGED')
        def check():
            fresh = open_absolute_directory('/run/lock', sticky_lock=True)
            try:
                require((os.fstat(fresh).st_dev, os.fstat(fresh).st_ino) ==
                        (os.fstat(parent).st_dev, os.fstat(parent).st_ino), 'LOCK_PARENT_CHANGED')
                require(metadata(os.fstat(fd)) == metadata(info) ==
                        metadata(os.stat('wavemesh-node.lock', dir_fd=fresh, follow_symlinks=False)), 'LOCK_PATH_CHANGED')
            finally:
                os.close(fresh)
        check()
        yield check
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def parse_unit(text):
    require(len(text.encode()) <= 16384, 'UNIT_OUTPUT_BOUND')
    values = {}
    for line in text.splitlines():
        require('=' in line, 'UNIT_PARSE')
        key, value = line.split('=', 1)
        require(key in UNIT_PROPS and key not in values, 'UNIT_PROPERTIES')
        values[key] = value
    require(set(values) == set(UNIT_PROPS), 'UNIT_PROPERTIES')
    return values


def checked_public_command(args, expected_sha256):
    """Exact binaries/arguments only. Errors never export their stdout/stderr."""
    require(args[0] in {'/usr/bin/systemctl', '/usr/bin/busctl'}, 'COMMAND_NOT_ALLOWED')
    require(type(expected_sha256) is str and re.fullmatch('[a-f0-9]{64}', expected_sha256), 'NATIVE_COMMAND_PIN_UNBOUND')
    parent = open_absolute_directory('/usr/bin')
    fd = None
    try:
        fd = os.open(Path(args[0]).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 0 and
                not info.st_mode & 0o022 and info.st_nlink == 1 and info.st_size <= MAX_PUBLIC_BYTES, 'UNSAFE_NATIVE_COMMAND')
        value, size = hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            size += len(chunk)
            require(size <= MAX_PUBLIC_BYTES, 'NATIVE_COMMAND_SIZE')
            value.update(chunk)
        require(value.hexdigest() == expected_sha256 and metadata(os.fstat(fd)) == metadata(info) ==
                metadata(os.stat(Path(args[0]).name, dir_fd=parent, follow_symlinks=False)), 'NATIVE_COMMAND_SOURCE_DRIFT')
        # Execute the pinned open ELF inode; a path swap cannot select new bytes.
        completed = subprocess.run(['/proc/self/fd/'+str(fd), *args[1:]], check=False,
                                   pass_fds=(fd,), capture_output=True, text=True, timeout=15,
                                   stdin=subprocess.DEVNULL, cwd='/',
                                   env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin', 'LANG':'C',
                                        'LC_ALL':'C', 'SYSTEMD_COLORS':'0', 'SYSTEMD_PAGER':'cat'})
        require(completed.returncode == 0 and not completed.stderr and len(completed.stdout.encode()) <= 16384, 'NATIVE_COMMAND_UNCERTAIN')
        return completed.stdout
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def namespace_identity():
    # Metadata of this process only; no environment, argv, config or namespace entry.
    mount = os.stat('/proc/self/ns/mnt')
    root = os.stat('/')
    return {'mount': [mount.st_dev, mount.st_ino], 'root': [root.st_dev, root.st_ino]}


def check_operator_source(expected):
    require(type(expected) is str and re.fullmatch('[a-f0-9]{64}', expected), 'OPERATOR_SOURCE_PIN_UNBOUND')
    require(os.path.abspath(__file__) == OPERATOR_PATH, 'OPERATOR_DEPLOYMENT_PATH')
    parent = open_absolute_directory(str(Path(OPERATOR_PATH).parent))
    fd = None
    try:
        fd = os.open(Path(OPERATOR_PATH).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == before.st_gid == 0 and
                not before.st_mode & 0o022 and before.st_nlink == 1 and before.st_size <= 128*1024,
                'UNSAFE_OPERATOR_SOURCE')
        data = os.read(fd, 128*1024+1)
        require(len(data) == before.st_size and digest(data) == expected and
                metadata(before) == metadata(os.fstat(fd)) ==
                metadata(os.stat(Path(OPERATOR_PATH).name, dir_fd=parent, follow_symlinks=False)),
                'OPERATOR_SOURCE_CHANGED')
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def require_cgroup2_filesystem(fd):
    """Only the reviewed Linux x86_64 LP64 statfs ABI; unsupported means STOP."""
    import ctypes
    require(sys.platform == 'linux' and ctypes.sizeof(ctypes.c_long) == 8 and
            ctypes.sizeof(ctypes.c_void_p) == 8 and os.uname().machine == 'x86_64',
            'CGROUP2_ABI_UNSUPPORTED')
    class StatFS(ctypes.Structure):
        _fields_ = [('f_type',ctypes.c_long), ('f_bsize',ctypes.c_long),
                    ('f_blocks',ctypes.c_ulong), ('f_bfree',ctypes.c_ulong),
                    ('f_bavail',ctypes.c_ulong), ('f_files',ctypes.c_ulong),
                    ('f_ffree',ctypes.c_ulong), ('f_fsid',ctypes.c_int*2),
                    ('f_namelen',ctypes.c_long), ('f_frsize',ctypes.c_long),
                    ('f_flags',ctypes.c_long), ('f_spare',ctypes.c_long*4)]
    require(ctypes.sizeof(StatFS) == 120, 'CGROUP2_ABI_UNSUPPORTED')
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.fstatfs
    call.argtypes, call.restype = [ctypes.c_int,ctypes.POINTER(StatFS)], ctypes.c_int
    value = StatFS()
    require(call(fd,ctypes.byref(value)) == 0, 'CGROUP2_FSTATFS_FAILED')
    require(value.f_type == 0x63677270, 'CGROUP2_FILESYSTEM_REQUIRED')


def parse_cgroup_fields(raw, required, optional=()):
    require(raw and len(raw) <= 32768 and raw.endswith(b'\n'), 'CGROUP_CONTROL_FORMAT')
    values = {}
    for line in raw.splitlines():
        parts = line.split(b' ')
        require(len(parts) == 2 and re.fullmatch(rb'[a-z_]+',parts[0]) and
                re.fullmatch(rb'(0|[1-9][0-9]{0,18})',parts[1]), 'CGROUP_CONTROL_FORMAT')
        key = parts[0].decode('ascii')
        require(key not in values and key in (*required,*optional), 'CGROUP_CONTROL_FIELDS')
        values[key] = int(parts[1])
    require(set(required) <= set(values), 'CGROUP_CONTROL_FIELDS')
    return values


class NativeRuntime:
    def __init__(self, admission, command_pins, activation_pins, operator_sha256):
        admission.assert_closed()
        require(sys.platform == 'linux' and os.geteuid() == 0, 'NATIVE_LINUX_ROOT_REQUIRED')
        check_operator_source(operator_sha256)
        require(set(command_pins) == {'/usr/bin/systemctl', '/usr/bin/busctl'}, 'NATIVE_COMMAND_PINS_UNBOUND')
        require(set(activation_pins) == set(UNITS) and all(set(p) == set(ACTIVATION_PROPS)
                for p in activation_pins.values()), 'UNIT_ACTIVATION_PINS_UNBOUND')
        self.admission = admission
        self.operator_sha256 = operator_sha256
        self.command_pins, self.activation_pins = dict(command_pins), activation_pins
        self.namespace = namespace_identity()
        self.old_processes = {}

    def assert_admission(self):
        self.admission.assert_closed()
        check_operator_source(self.operator_sha256)
        require(namespace_identity() == self.namespace, 'OPERATOR_NAMESPACE_CHANGED')

    def operation_binding(self, document):
        self.assert_admission()
        binding = {'target': document['target'], 'operation': document['operation'],
                   'manifest_sha256': digest(canonical(document)),
                   'operator_sha256': self.operator_sha256, 'namespace': self.namespace}
        # Future concrete adapter must bind actual target/API/source/operation.
        # This is not a user-supplied receipt or a Boolean quiescence argument.
        self.admission.assert_target_binding(binding)
        return binding

    def unit(self, name):
        require(name in UNITS, 'UNIT_NOT_ALLOWED')
        values = parse_unit(checked_public_command(['/usr/bin/systemctl', 'show', name, '--no-pager',
                         '--property=' + ','.join(UNIT_PROPS)], self.command_pins['/usr/bin/systemctl']))
        require(values['Id'] == name and values['LoadState'] == 'loaded' and
                values['NeedDaemonReload'] == 'no', 'UNIT_IDENTITY_OR_RELOAD')
        require(values['KillMode'] == 'control-group' and values['SendSIGKILL'] == 'yes', 'UNSUPPORTED_STOP_POLICY')
        require(values['FileDescriptorStoreMax'] == values['NFileDescriptorStore'] == '0', 'FDSTORE_NOT_CLOSED')
        require(values['FileDescriptorStorePreserve'] in {'no', 'restart'}, 'FDSTORE_POLICY_UNKNOWN')
        require(all(values[k] == '' for k in ('TriggeredBy', 'Triggers', 'OnFailure', 'OnSuccess')), 'UNSUPPORTED_ACTIVATION_PATH')
        require(values['RefuseManualStop'] == values['RefuseManualStart'] == 'no', 'UNSUPPORTED_MANUAL_POLICY')
        require({k: values[k] for k in ACTIVATION_PROPS} == self.activation_pins[name], 'UNIT_ACTIVATION_DRIFT')
        require(values['ActiveState'] in {'active', 'inactive', 'deactivating', 'activating'} and
                values['MainPID'].isdigit() and values['ControlPID'].isdigit() and values['Job'].isdigit(), 'UNIT_STATE_UNKNOWN')
        return values

    def members(self, group):
        """Finite bracket under held admission, not an atomic/forever-empty claim.

        Only exact known leaf absence can mean empty. Mandatory ancestors must
        exist; descendants/dying descendants are unsupported, never skipped.
        Kernel populated covers recursive live processes; procs supplies direct
        baseline PIDs and cannot independently establish emptiness.
        """
        require(group in {'/system.slice/'+name for name in UNITS}, 'CGROUP_PATH')
        try:
            with contextlib.ExitStack() as stack:
                root = open_absolute_directory('/sys/fs/cgroup')
                stack.callback(os.close,root)
                root_info = os.fstat(root)

                def checked(fd, directory):
                    value = os.fstat(fd)
                    require_cgroup2_filesystem(fd)
                    require(value.st_dev == root_info.st_dev, 'CGROUP_FILESYSTEM_CHANGED')
                    if directory:
                        safe_root_directory(value)
                    else:
                        require(stat.S_ISREG(value.st_mode) and value.st_uid == value.st_gid == 0 and
                                not value.st_mode & 0o022 and value.st_nlink == 1, 'CGROUP_CONTROL_UNSAFE')
                    return value

                checked(root,True)
                def opened(name, parent, directory, *, absent_ok=False):
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                    if directory:
                        flags |= os.O_DIRECTORY
                    try:
                        fd = os.open(name,flags,dir_fd=parent)
                    except FileNotFoundError:
                        if absent_ok:
                            return None,None
                        raise
                    stack.callback(os.close,fd)
                    before = checked(fd,directory)
                    require(metadata(before) == metadata(os.stat(name,dir_fd=parent,follow_symlinks=False)),
                            'CGROUP_NAMED_BINDING_CHANGED')
                    return fd,before

                parent,parent_info = opened('system.slice',root,True)
                leaf_name = group.rsplit('/',1)[1]
                leaf = None
                files = {}

                def bindings():
                    fresh = open_absolute_directory('/sys/fs/cgroup')
                    stack.callback(os.close,fresh)
                    require(metadata(checked(fresh,True)) == metadata(root_info) == metadata(checked(root,True)),
                            'CGROUP_ROOT_CHANGED')
                    require(metadata(checked(parent,True)) == metadata(parent_info) ==
                            metadata(os.stat('system.slice',dir_fd=root,follow_symlinks=False)), 'CGROUP_PARENT_CHANGED')
                    if leaf is not None:
                        require(metadata(checked(leaf,True)) == metadata(leaf_info) ==
                                metadata(os.stat(leaf_name,dir_fd=parent,follow_symlinks=False)), 'CGROUP_LEAF_CHANGED')
                    for name,(fd,pin) in files.items():
                        require(metadata(checked(fd,False)) == metadata(pin) ==
                                metadata(os.stat(name,dir_fd=leaf,follow_symlinks=False)), 'CGROUP_CONTROL_CHANGED')

                leaf,leaf_info = opened(leaf_name,parent,True,absent_ok=True)
                if leaf is None:
                    # Parent/root disappearance is never absence authority.
                    bindings()
                    try:
                        os.stat(leaf_name,dir_fd=parent,follow_symlinks=False)
                    except FileNotFoundError:
                        bindings()
                        return set()
                    raise Stop('CGROUP_LEAF_APPEARED')

                for name in ('cgroup.stat','cgroup.events','cgroup.procs'):
                    files[name] = opened(name,leaf,False)

                def read(name):
                    fd,_ = files[name]
                    os.lseek(fd,0,os.SEEK_SET)
                    raw = os.read(fd,32769)
                    require(len(raw) <= 32768 and os.read(fd,1) == b'', 'CGROUP_CONTROL_READ_BOUND')
                    return raw

                def snapshot():
                    counters = parse_cgroup_fields(read('cgroup.stat'),('nr_descendants','nr_dying_descendants'))
                    require(not any(counters.values()), 'CGROUP_SUBTREE_UNSUPPORTED')
                    events = parse_cgroup_fields(read('cgroup.events'),('populated',),('frozen',))
                    require(all(value in {0,1} for value in events.values()), 'CGROUP_EVENT_VALUE')
                    raw = read('cgroup.procs')
                    require(not raw or raw.endswith(b'\n'), 'CGROUP_PROCS_FORMAT')
                    lines = raw.splitlines()
                    require(len(lines) <= 256 and all(re.fullmatch(rb'[1-9][0-9]{0,9}',line) for line in lines),
                            'CGROUP_PROCS_FORMAT')
                    members = {int(line) for line in lines}
                    require(len(members) == len(lines) and all(pid <= 2147483647 for pid in members), 'CGROUP_PROCS_FORMAT')
                    require(bool(events['populated']) == bool(members), 'CGROUP_POPULATION_UNCERTAIN')
                    return counters,events,members

                before = snapshot()
                bindings()
                after = snapshot()
                bindings()
                require(before == after, 'CGROUP_OBSERVATION_CHANGED')
                return after[2]
        except Stop:
            raise
        except (OSError,ValueError,AttributeError):
            raise Stop('CGROUP_OBSERVATION_UNKNOWN') from None

    def capture_baseline(self):
        self.assert_admission()
        baseline = {}
        for name in UNITS:
            v = self.unit(name)
            require(v['Job'] == v['ControlPID'] == '0' and ((v['ActiveState'], v['SubState']) in
                    {('active', 'running'), ('inactive', 'dead')}), 'BASELINE_JOB_OR_STATE')
            expected = '/system.slice/' + name
            require(v['ControlGroup'] in {'', expected}, 'CGROUP_PATH')
            members = self.members(expected)
            for pid in members:
                info = os.stat('/proc/' + str(pid))
                self.old_processes[pid] = (info.st_dev, info.st_ino, info.st_ctime_ns)
            require(v['MainPID'] == '0' or int(v['MainPID']) in members, 'MAINPID_OUTSIDE_CGROUP')
            baseline[name] = {'active': v['ActiveState'] == 'active', 'group': expected}
        return baseline

    def request(self, action, name, journal, *, reserve=False):
        require(action in {'StopUnit', 'StartUnit'} and name in UNITS, 'SERVICE_REQUEST')
        self.assert_admission()
        # Intent exists before an ambiguous transport result. No automatic replay.
        key = action + ':' + name + (':reserve' if reserve else '')
        if reserve:
            require(action == 'StartUnit', 'RECOVERY_STOP_FORBIDDEN')
            require(any(r.get('request') == action+':'+name for r in journal.records), 'RECOVERY_WITHOUT_PRIOR_START')
            latest = [r for r in journal.records if r.get('event') == 'SERVICE_RECONCILED' and r.get('unit') == name]
            reconciled = [i for i,r in enumerate(journal.records) if r.get('event') == 'SERVICE_RECONCILED' and r.get('unit') == name]
            prior = [i for i,r in enumerate(journal.records) if r.get('request', '').startswith(action+':'+name)]
            values = self.unit(name)
            require(latest and latest[-1]['state'] == 'inactive' and reconciled[-1] > max(prior) and
                    values['Job'] == values['MainPID'] == values['ControlPID'] == '0' and
                    (values['ActiveState'], values['SubState']) == ('inactive','dead') and
                    not self.members('/system.slice/'+name), 'RECOVERY_RECONCILIATION_REQUIRED')
            self.admission.assert_corrective_start_permitted(name)
        require(not any(r.get('request') == key for r in journal.records), 'SERVICE_REQUEST_ALREADY_RECORDED')
        journal.append('SERVICE_INTENT', request=key)
        try:
            output = checked_public_command(['/usr/bin/busctl', '--system', 'call', 'org.freedesktop.systemd1',
                '/org/freedesktop/systemd1', 'org.freedesktop.systemd1.Manager', action, 'ss', name, 'fail'], self.command_pins['/usr/bin/busctl'])
            match = re.fullmatch(r'o "(/org/freedesktop/systemd1/job/[1-9][0-9]*)"\s*', output)
            require(match is not None, 'SERVICE_JOB_HANDLE_UNKNOWN')
            journal.append('SERVICE_JOB', request=key, job=match.group(1))
            return match.group(1)
        except Exception:
            raise Stop('SERVICE_OUTCOME_UNKNOWN_RECONCILE') from None

    def observe_job(self, name, job, *, active, deadline):
        require(re.fullmatch(r'/org/freedesktop/systemd1/job/[1-9][0-9]*', job), 'SERVICE_JOB_HANDLE')
        while True:
            self.assert_admission()
            values = self.unit(name)
            require(values['Job'] in {'0', job.rsplit('/', 1)[1]}, 'UNEXPECTED_SERVICE_JOB')
            if values['Job'] == '0':
                require(values['ControlPID'] == '0' and (values['ActiveState'], values['SubState']) ==
                        (('active', 'running') if active else ('inactive', 'dead')), 'SERVICE_TERMINAL_MISMATCH')
                return
            require(time.monotonic() < deadline, 'SERVICE_JOB_DEADLINE_RECONCILE')
            time.sleep(0.25)

    def assert_quiescent(self):
        self.assert_admission()
        for name in UNITS:
            values = self.unit(name)
            require(values['Job'] == values['MainPID'] == values['ControlPID'] == '0' and values['ActiveState'] == 'inactive' and
                    values['SubState'] == 'dead', 'UNIT_NOT_QUIESCENT')
            require(not self.members('/system.slice/' + name), 'CGROUP_NOT_EMPTY')
        for pid, identity in self.old_processes.items():
            try:
                info = os.stat('/proc/' + str(pid))
            except FileNotFoundError:
                continue
            require((info.st_dev, info.st_ino, info.st_ctime_ns) != identity, 'OLD_PROCESS_STILL_EXISTS')
        self.assert_admission()


class UnboundAdmission:
    """Not a Boolean or user receipt. A concrete reviewed live adapter is required."""
    def assert_closed(self):
        raise Stop('LIVE_ADMISSION_UNBOUND_NOT_READY')


def open_native_journal(document, runtime):
    """Only construct a protected ledger after live admission and source binding."""
    validate_manifest(document)
    runtime.assert_admission()
    binding = runtime.operation_binding(document)
    journal = Journal(JOURNAL_ROOT + '/' + document['operation'], binding)
    try:
        runtime.assert_admission()
    except Exception:
        journal.close()
        raise
    return journal


def run_window(document, runtime, objects_factory, journal, *, deadline):
    """Future integration seam. Journal/service effects require a bound live adapter.

    External operational ownership is a human-confirmed trust assumption; runtime
    observations and the reviewed command/transaction gate are independent facts.
    This function never opens a gate or installs/removes a systemd condition.
    """
    validate_manifest(document)
    runtime.assert_admission()  # before lock, ledger creation, service or owner effects
    require(journal.binding == runtime.operation_binding(document), 'WINDOW_INPUT_BINDING')
    baseline = runtime.capture_baseline()
    require(not journal.records or not any(r['event'] == 'WINDOW' for r in journal.records), 'WINDOW_ALREADY_STARTED_RECONCILE')
    # Existing participating lock, not a new lease. Hold through independent verify.
    with existing_mutation_lock() as check_lock:
        check_lock()
        journal.append('WINDOW', baseline=baseline, namespace=runtime.namespace,
                       old_processes={str(pid): list(identity) for pid, identity in runtime.old_processes.items()})
        def closed():
            check_lock()
            runtime.assert_quiescent()
            check_lock()
        for name in UNITS:
            if baseline[name]['active']:
                job = runtime.request('StopUnit', name, journal)
                runtime.observe_job(name, job, active=False, deadline=deadline)
        closed()
        objects = objects_factory(document)
        try:
            apply_owner_only(document, objects, journal, closed)
        finally:
            objects.close()
        independent = objects_factory(document)
        try:
            count = verify_objects(document, independent, closed)
        finally:
            independent.close()
        journal.append('FILESYSTEM_ACCEPTED', count=count)
    # Admission stays closed. Ordinary lifecycle effects need the future grant.
    restore_window(document, runtime, objects_factory, journal, deadline=deadline)
    return 'FILESYSTEM_AND_RESTORATION_ACCEPTED_COMMAND_RESUME_NOT_PERFORMED'


def window_baseline(runtime, journal):
    records = [r for r in journal.records if r.get('event') == 'WINDOW']
    require(len(records) == 1, 'WINDOW_LEDGER_REQUIRED')
    record = records[0]
    require(record['namespace'] == runtime.namespace, 'RECOVERY_NAMESPACE_CHANGED')
    baseline = record['baseline']
    require(set(baseline) == set(UNITS), 'BASELINE_UNIT_SET')
    for name, value in baseline.items():
        require(set(value) == {'active', 'group'} and type(value['active']) is bool and
                value['group'] == '/system.slice/'+name, 'BASELINE_INVALID')
    processes = record['old_processes']
    require(type(processes) is dict and len(processes) <= 256, 'PROCESS_BASELINE_BOUND')
    runtime.old_processes = {}
    for pid, identity in processes.items():
        require(pid.isdigit() and int(pid) > 0 and type(identity) is list and len(identity) == 3 and
                all(type(v) is int and v >= 0 for v in identity), 'PROCESS_BASELINE_INVALID')
        runtime.old_processes[int(pid)] = tuple(identity)
    return baseline


def reconcile_window(runtime, journal):
    """Live observations only: never reissue an uncertain stop/start."""
    runtime.assert_admission()
    baseline = window_baseline(runtime, journal)
    observations = {}
    for name in UNITS:
        values = runtime.unit(name)
        require(values['Job'] == '0', 'LIVE_JOB_OBSERVE_DO_NOT_REPEAT')
        require((values['ActiveState'], values['SubState']) in
                {('active', 'running'), ('inactive', 'dead')}, 'SERVICE_STATE_UNKNOWN')
        if values['ActiveState'] == 'inactive':
            require(values['MainPID'] == values['ControlPID'] == '0' and
                    not runtime.members(baseline[name]['group']), 'SERVICE_NOT_DRAINED')
        journal.append('SERVICE_RECONCILED', unit=name, state=values['ActiveState'])
        observations[name] = values['ActiveState']
    return observations


def restore_window(document, runtime, objects_factory, journal, *, deadline, reserve=False):
    """Explicit recovery/restore call; partial root subset is never reverted.

    Fresh independent same-object reconciliation is enforced before any start.
    Gate cleanup/resume is never inferred or performed by this routine.
    """
    runtime.assert_admission()
    validate_manifest(document)
    require(journal.binding == runtime.operation_binding(document), 'WINDOW_INPUT_BINDING')
    baseline = window_baseline(runtime, journal)
    if any('path' in r for r in journal.records):
        independent = objects_factory(document)
        try:
            # Restoration may already have accepted one known unit before a
            # later start failed. No further owner mutation is performed here;
            # the live admission/ownership boundary stays held during readback.
            reconcile_objects(document, independent, journal, runtime.assert_admission)
        finally:
            independent.close()
    runtime.admission.assert_restore_permitted()
    # x-ui first. Agent startup is a separate lifecycle-aware admission assertion.
    for name in reversed(UNITS):
        values = runtime.unit(name)
        require(values['Job'] == '0', 'LIVE_JOB_OBSERVE_DO_NOT_REPEAT')
        if not baseline[name]['active']:
            require((values['ActiveState'], values['SubState']) == ('inactive', 'dead') and
                    values['MainPID'] == values['ControlPID'] == '0' and
                    not runtime.members(baseline[name]['group']), 'BASELINE_INACTIVE_CHANGED')
            continue
        if name == 'wavemesh-node-agent.service':
            runtime.admission.assert_agent_restore_permitted()
        if values['ActiveState'] == 'inactive':
            job = runtime.request('StartUnit', name, journal, reserve=reserve)
            runtime.observe_job(name, job, active=True, deadline=deadline)
        else:
            require((values['ActiveState'], values['SubState']) == ('active', 'running'), 'RESTORE_STATE_UNKNOWN')
        if name == 'x-ui.service':
            runtime.admission.assert_panel_restored()
    runtime.admission.assert_restored_and_cleanup_accepted()
    complete = any(r['event'] == 'FILESYSTEM_ACCEPTED' for r in journal.records)
    journal.append('RESTORED_COMMAND_GATES_UNCHANGED', filesystemAccepted=complete)
    return 'RESTORED' if complete else 'PARTIAL_RESTORED_RECONCILIATION_REQUIRED'


def resume_owner_interval(document, runtime, objects_factory, journal):
    """Explicit continuation after reconciliation; never requests another stop."""
    validate_manifest(document)
    require(journal.binding == runtime.operation_binding(document), 'WINDOW_INPUT_BINDING')
    window_baseline(runtime, journal)
    runtime.assert_quiescent()
    with existing_mutation_lock() as check_lock:
        def closed():
            check_lock()
            runtime.assert_quiescent()
            check_lock()
        objects = objects_factory(document)
        try:
            apply_owner_only(document, objects, journal, closed)
        finally:
            objects.close()
        independent = objects_factory(document)
        try:
            count = verify_objects(document, independent, closed)
        finally:
            independent.close()
        journal.append('FILESYSTEM_ACCEPTED', count=count)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Do not parse manifests, inspect a host, acquire a lock, or run a command before
    # a concrete production live-admission adapter has been reviewed and bound.
    code = 'LIVE_ADMISSION_UNBOUND_NOT_READY' if '--execute' in argv else 'INERT_SOURCE_PREPARATION_ONLY'
    print(json.dumps({'status': code, 'exit': 77, 'effects': 0}))
    return 77


if __name__ == '__main__':
    raise SystemExit(main())
