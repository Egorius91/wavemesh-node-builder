#!/usr/bin/env python3
"""Bounded local transaction-status observation, never LIVE repair admission.

Default77. Explicit --observe-readonly consumes a previously admitted root-private
binding on stdin. No root discovery, recovery, lock write or service action.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time

if __package__:
    from . import xui_owner_repair as owner
else:
    import xui_owner_repair as owner

MAX_BINDING_BYTES = 16384
MAX_ROOTS = 4
MAX_DEPTH = 16
MAX_TRANSACTIONS = 128  # Total across all roots, not a per-root multiplier.
MAX_METADATA_BYTES = 4096
MAX_PATH_BYTES = 512
MAX_NAME_BYTES = 128
OBSERVATION_SECONDS = 5
TERMINAL = ('committed', 'rolled_back')
ACTIVE = ('in_progress', 'recovering', 'rollback_failed')
STOP_CODES = frozenset((
    'STOP_ARGUMENTS', 'STOP_NATIVE_LINUX_ROOT_REQUIRED', 'STOP_BINDING_SCHEMA',
    'STOP_TARGET_OPERATION', 'STOP_NAMESPACE_BINDING', 'STOP_ROOT_BINDING',
    'STOP_BINDING_HASH', 'STOP_BINDING_INPUT', 'STOP_JSON', 'STOP_DUPLICATE_FIELD',
    'STOP_UNSAFE_DIRECTORY', 'STOP_DIRECTORY_CHANGED', 'STOP_NAMESPACE_CHANGED',
    'STOP_ROOT_IDENTITY', 'STOP_TRANSACTION_LIMIT', 'STOP_TRANSACTION_NAME',
    'STOP_UNSAFE_METADATA', 'STOP_METADATA_SIZE', 'STOP_METADATA_CHANGED',
    'STOP_PLAN_SCHEMA', 'STOP_RESULT_SCHEMA', 'STOP_TRANSACTION_NONTERMINAL',
    'STOP_TRANSACTION_STATUS_UNKNOWN', 'STOP_OBSERVATION_TIMEOUT',
    'STOP_OBSERVATION_IO', 'STOP_OBSERVATION_UNKNOWN'))


class Stop(RuntimeError):
    def __init__(self, code):
        self.code = code if code in STOP_CODES else 'STOP_OBSERVATION_UNKNOWN'
        super().__init__(self.code)


def require(condition, code):
    if not condition:
        raise Stop(code)


def pairs(items):
    value = {}
    for key, item in items:
        require(key not in value, 'STOP_DUPLICATE_FIELD')
        value[key] = item
    return value


def parse_json(raw, limit):
    require(type(raw) is bytes and 0 < len(raw) <= limit, 'STOP_JSON')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise Stop('STOP_JSON') from None


def hex_string(value, length):
    return type(value) is str and re.fullmatch('[a-f0-9]{%d}' % length, value) is not None


def identity(value):
    return (type(value) is list and len(value) == 2 and
            all(type(n) is int and 0 <= n <= 2**64-1 for n in value) and value[1] > 0)


def validate_binding(value, target, operation):
    require(type(value) is dict and set(value) ==
            {'schema', 'target', 'operation', 'namespace', 'roots'} and
            type(value['schema']) is int and value['schema'] == 1, 'STOP_BINDING_SCHEMA')
    require(type(target) is str and target in owner.TARGETS and hex_string(operation, 32) and
            value['target'] == target and value['operation'] == operation, 'STOP_TARGET_OPERATION')
    namespace = value['namespace']
    require(type(namespace) is dict and set(namespace) == {'mount', 'root'} and
            all(identity(namespace[k]) for k in namespace), 'STOP_NAMESPACE_BINDING')
    roots = value['roots']
    require(type(roots) is list and 1 <= len(roots) <= MAX_ROOTS, 'STOP_ROOT_BINDING')
    seen_paths, seen_ids, ancestors = [], set(), {}
    for root in roots:
        require(type(root) is dict and set(root) == {'path', 'chain'} and
                type(root['path']) is str, 'STOP_ROOT_BINDING')
        path = root['path']
        components = path.split('/')[1:]
        require(path.startswith('/') and len(path.encode('utf-8')) <= MAX_PATH_BYTES and
                1 <= len(components) <= MAX_DEPTH and
                all(p not in ('', '.', '..') and '\x00' not in p and
                    len(p.encode('utf-8')) <= MAX_NAME_BYTES for p in components), 'STOP_ROOT_BINDING')
        chain = root['chain']
        require(type(chain) is list and len(chain) == len(components)+1 and
                all(identity(pin) for pin in chain) and chain[0] == namespace['root'], 'STOP_ROOT_BINDING')
        require(all(not (path == old or path.startswith(old+'/') or old.startswith(path+'/'))
                    for old in seen_paths) and tuple(chain[-1]) not in seen_ids, 'STOP_ROOT_BINDING')
        for i, pin in enumerate(chain):
            prefix = '/' + '/'.join(components[:i])
            require(prefix not in ancestors or ancestors[prefix] == pin, 'STOP_ROOT_BINDING')
            ancestors[prefix] = pin
        seen_paths.append(path)
        seen_ids.add(tuple(chain[-1]))
    return value


def metadata_status(plan, result):
    # This is the redacted CLI metadata producer, not config/backup/request data.
    require(type(plan) is dict and {'schema_version'} <= set(plan) <=
            {'schema_version', 'operation', 'pid', 'started_at'} and
            type(plan['schema_version']) is int and plan['schema_version'] == 1, 'STOP_PLAN_SCHEMA')
    # Preserve the existing Agent's minimal schema1 terminal fixture. Optional
    # current CLI fields are type/bounds checked when present; payload is refused.
    require('operation' not in plan or (type(plan['operation']) is str and len(plan['operation']) <= 80),
            'STOP_PLAN_SCHEMA')
    require('pid' not in plan or (type(plan['pid']) is int and 0 < plan['pid'] <= 2**31-1), 'STOP_PLAN_SCHEMA')
    require('started_at' not in plan or (type(plan['started_at']) is str and
            re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', plan['started_at'])), 'STOP_PLAN_SCHEMA')
    require(type(result) is dict and {'status'} <= set(result) <= {'status', 'finished_at', 'message'} and
            type(result['status']) is str, 'STOP_RESULT_SCHEMA')
    require('message' not in result or (type(result['message']) is str and len(result['message']) <= 160),
            'STOP_RESULT_SCHEMA')
    require('finished_at' not in result or (type(result['finished_at']) is str and
            re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', result['finished_at'])), 'STOP_RESULT_SCHEMA')
    require(result['status'] not in ACTIVE, 'STOP_TRANSACTION_NONTERMINAL')
    require(result['status'] in TERMINAL, 'STOP_TRANSACTION_STATUS_UNKNOWN')
    return result['status']


def require_native():
    require(sys.platform == 'linux' and os.geteuid() == 0, 'STOP_NATIVE_LINUX_ROOT_REQUIRED')


class NativeStatus:
    """Own finite no-follow descriptors until one full final verification; no writes."""
    def __init__(self, binding):
        require_native()
        self.binding = binding
        self.deadline = time.monotonic() + OBSERVATION_SECONDS
        self.fds, self.directories, self.files, self.roots = [], [], [], []

    def __enter__(self):
        try:
            self.check_namespace()
            for root in self.binding['roots']:
                parent = None
                chain = []
                for name, pin in zip(('/', *root['path'].split('/')[1:]), root['chain']):
                    record = self.directory(parent, name)
                    require(list(record[3][:2]) == pin, 'STOP_ROOT_IDENTITY')
                    chain.append(record)
                    parent = record[0]
                self.roots.append((parent, chain, None))
            self.verify()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, kind, *_):
        try:
            self.close()
        except Stop:
            if kind is None:
                raise

    def close(self):
        failed = False
        while self.fds:
            try:
                os.close(self.fds.pop())
            except OSError:
                failed = True
        require(not failed, 'STOP_OBSERVATION_IO')

    def tick(self):
        require(time.monotonic() <= self.deadline, 'STOP_OBSERVATION_TIMEOUT')

    def check_namespace(self):
        self.tick()
        require(owner.namespace_identity() == self.binding['namespace'], 'STOP_NAMESPACE_CHANGED')

    def directory(self, parent, name):
        self.tick()
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC |
                     os.O_NONBLOCK | os.O_NOATIME, dir_fd=parent)
        self.fds.append(fd)
        info = os.fstat(fd)
        try:
            owner.safe_root_directory(info)
        except owner.Stop:
            raise Stop('STOP_UNSAFE_DIRECTORY') from None
        record = (fd, parent, name, owner.metadata(info))
        self.directories.append(record)
        self.check_record(record, 'STOP_DIRECTORY_CHANGED')
        return record

    def check_record(self, record, code):
        self.tick()
        fd, parent, name, pin = record
        require(owner.metadata(os.fstat(fd)) == pin and
                owner.metadata(os.stat(name, dir_fd=parent, follow_symlinks=False)) == pin, code)

    def names(self, fd, remaining):
        # A fresh open description avoids reusing scandir's directory offset.
        self.tick()
        scan = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC |
                       os.O_NONBLOCK | os.O_NOATIME, dir_fd=fd)
        try:
            names = []
            with os.scandir(scan) as entries:
                for entry in entries:
                    self.tick()
                    require(len(names) < remaining, 'STOP_TRANSACTION_LIMIT')
                    name = entry.name
                    require(type(name) is str and name not in ('', '.', '..') and
                            '/' not in name and '\x00' not in name and
                            len(name.encode('utf-8')) <= MAX_NAME_BYTES, 'STOP_TRANSACTION_NAME')
                    names.append(name)
            return sorted(names)
        finally:
            os.close(scan)

    def read_metadata(self, parent, name):
        self.tick()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOATIME,
                     dir_fd=parent)
        self.fds.append(fd)
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 0 and
                stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, 'STOP_UNSAFE_METADATA')
        require(0 < info.st_size <= MAX_METADATA_BYTES, 'STOP_METADATA_SIZE')
        record = (fd, parent, name, owner.metadata(info))
        self.files.append(record)
        self.check_record(record, 'STOP_METADATA_CHANGED')
        raw = bytearray()
        while True:
            self.tick()
            chunk = os.read(fd, min(1024, MAX_METADATA_BYTES+1-len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            require(len(raw) <= MAX_METADATA_BYTES, 'STOP_METADATA_SIZE')
        require(len(raw) == info.st_size, 'STOP_METADATA_CHANGED')
        self.check_record(record, 'STOP_METADATA_CHANGED')
        return parse_json(bytes(raw), MAX_METADATA_BYTES)

    def verify(self):
        self.check_namespace()
        for record in self.directories:
            self.check_record(record, 'STOP_DIRECTORY_CHANGED')
        for record in self.files:
            self.check_record(record, 'STOP_METADATA_CHANGED')
        for fd, _, names in self.roots:
            if names is not None:
                require(self.names(fd, MAX_TRANSACTIONS) == names, 'STOP_DIRECTORY_CHANGED')
        for record in self.directories:
            self.check_record(record, 'STOP_DIRECTORY_CHANGED')
        for record in self.files:
            self.check_record(record, 'STOP_METADATA_CHANGED')
        self.check_namespace()

    def observe(self):
        counts = {'rootsObserved': len(self.roots), 'transactionsObserved': 0, 'committed': 0, 'rolledBack': 0}
        for i, (fd, chain, _) in enumerate(self.roots):
            names = self.names(fd, MAX_TRANSACTIONS-counts['transactionsObserved'])
            self.roots[i] = (fd, chain, names)
            for name in names:
                child = self.directory(fd, name)[0]
                status = metadata_status(self.read_metadata(child, 'plan.json'), self.read_metadata(child, 'result.json'))
                counts['transactionsObserved'] += 1
                counts['committed' if status == 'committed' else 'rolledBack'] += 1
        self.verify()
        return {'status': 'LOCAL_TRANSACTIONS_CLEAR_NOT_ADMISSION', 'counts': counts,
                'rootSetCompleteness': 'NOT_OBSERVED', 'operationalExclusion': 'NOT_OBSERVED',
                'liveAdmission': False, 'filesystemWrites': 0}


def observe(binding, target, operation):
    try:
        validate_binding(binding, target, operation)
        with NativeStatus(binding) as reader:
            return reader.observe()
    except OSError:
        raise Stop('STOP_OBSERVATION_IO') from None
    except (ValueError, UnicodeError, RecursionError):
        raise Stop('STOP_OBSERVATION_UNKNOWN') from None


def read_binding(expected_hash, target, operation):
    require(hex_string(expected_hash, 64), 'STOP_BINDING_HASH')
    info = os.fstat(0)
    require(stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 0 and
            stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1 and
            0 < info.st_size <= MAX_BINDING_BYTES, 'STOP_BINDING_INPUT')
    raw = sys.stdin.buffer.read(MAX_BINDING_BYTES+1)
    require(hashlib.sha256(raw).hexdigest() == expected_hash, 'STOP_BINDING_HASH')
    require(owner.metadata(os.fstat(0)) == owner.metadata(info) and len(raw) == info.st_size, 'STOP_BINDING_INPUT')
    return validate_binding(parse_json(raw, MAX_BINDING_BYTES), target, operation)


class Arguments(argparse.ArgumentParser):
    def error(self, _):
        raise Stop('STOP_ARGUMENTS')


def main(argv=None):
    try:
        parser = Arguments(description=__doc__, add_help=False)
        parser.add_argument('--observe-readonly', action='store_true')
        parser.add_argument('--target')
        parser.add_argument('--operation')
        parser.add_argument('--binding-sha256')
        args = parser.parse_args(argv)
        if not args.observe_readonly:
            print('{"status":"INERT_READONLY_ACTION_REQUIRED","filesystemWrites":0,"liveAdmission":false}')
            return 77
        require_native()
        binding = read_binding(args.binding_sha256, args.target, args.operation)
        print(json.dumps(observe(binding, args.target, args.operation), sort_keys=True, separators=(',', ':')))
        return 0
    except Exception:
        error = sys.exc_info()[1]
        code = (error.code if isinstance(error, Stop) and type(error.code) is str and error.code in STOP_CODES
                else 'STOP_OBSERVATION_IO' if isinstance(error, OSError) else 'STOP_OBSERVATION_UNKNOWN')
        print(json.dumps({'status': code, 'liveAdmission': False, 'filesystemWrites': 0}, separators=(',', ':')))
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
