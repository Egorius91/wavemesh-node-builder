"""One operation-bound recovery activation under retained locks and isolation.

Internal installer API, not an Agent command or a general service-start wrapper.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import sys
import time

import panel_request_guard as journal
from panel_isolation import PanelIsolation, bound_policy, verify_readback
from panel_stop import PanelStop, StopError

PANEL_BINARY = Path('/usr/local/x-ui/x-ui')
PANEL_ARGV = [str(PANEL_BINARY)]


class StartError(RuntimeError):
    pass


def property_data(value, signature):
    if not isinstance(value, dict) or set(value) != {'type', 'data'} or value['type'] != signature:
        raise StartError('START_PROPERTY_INVALID')
    return value['data']


def file_digest(path):
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise StartError('START_EXECUTABLE_UNSAFE')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & 0o022 or not info.st_mode & 0o100 or info.st_size > 268435456):
            raise StartError('START_EXECUTABLE_UNSAFE')
        digest = hashlib.sha256()
        while block := os.read(fd, 1024 * 1024):
            digest.update(block)
        return digest.hexdigest()
    finally:
        os.close(fd)


class PanelStart(PanelStop):
    def start_contract(self, observed, helper_sha256, executable_sha256):
        base = self.contract(observed, helper_sha256)
        expected = {'Type': 'simple', 'Restart': 'no', 'PIDFile': '',
                    'WorkingDirectory': str(PANEL_BINARY.parent)}
        for name, value in expected.items():
            if property_data(self.property(name), 's') != value:
                raise StartError('START_UNIT_UNSUPPORTED')
        entries = property_data(self.property('ExecStart'), 'a(sasbttttuii)')
        if (not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], list)
                or len(entries[0]) != 10 or entries[0][:3] != [str(PANEL_BINARY), PANEL_ARGV, False]
                or type(entries[0][2]) is not bool
                or any(type(number) is not int for number in entries[0][3:])):
            raise StartError('START_EXECUTABLE_UNSUPPORTED')
        if file_digest(PANEL_BINARY) != executable_sha256:
            raise StartError('START_EXECUTABLE_MISMATCH')
        return hashlib.sha256(json.dumps([base, expected, entries[0][:3], executable_sha256],
                                        sort_keys=True).encode()).hexdigest()

    def dispatch_start(self):
        # The returned job, not a successful process exit, identifies dispatch.
        raw = self.run(['/usr/bin/busctl', '--system', '--json=short', 'call',
                        'org.freedesktop.systemd1', '/org/freedesktop/systemd1',
                        'org.freedesktop.systemd1.Manager', 'StartUnit', 'ss', journal.PANEL_UNIT, 'fail'])
        data = property_data(json.loads(raw, object_pairs_hook=journal.unique_object), 'o')
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], str) or not re.fullmatch(
                '/org/freedesktop/systemd1/job/[1-9][0-9]*', data[0]):
            raise StartError('START_DISPATCH_UNCERTAIN')
        return data[0]

    def job(self):
        value = property_data(self.property('Job', 'Unit'), '(uo)')
        if (not isinstance(value, list) or len(value) != 2 or type(value[0]) is not int
                or not isinstance(value[1], str) or value[0] < 0
                or value[1] != ('/org/freedesktop/systemd1/job/' + str(value[0]) if value[0] else '/')):
            raise StartError('START_JOB_INVALID')
        return value[1]

    def peer_identity(self, connection, job_path):
        pid, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != 0 or pid <= 0:
            raise StartError('START_PEER_REJECTED')
        fd = os.pidfd_open(pid)
        try:
            observed = self.observe()
            group = '/system.slice/' + journal.PANEL_UNIT
            if (observed['ActiveState'] != 'activating' or observed['SubState'] != 'start-pre'
                    or observed['ControlPID'] != str(pid) or observed['MainPID'] != '0'
                    or observed['ControlGroup'] != group
                    or not re.fullmatch('[a-f0-9]{32}', observed['InvocationID'])
                    or self.job() != job_path):
                raise StartError('START_PEER_REJECTED')
            # Kernel cgroup and namespace identity supplement manager state.
            if ('0::' + group) not in Path('/proc/' + str(pid) + '/cgroup').read_text().splitlines():
                raise StartError('START_PEER_REJECTED')
            actual, current = os.stat('/proc/' + str(pid) + '/ns/net'), os.stat('/proc/self/ns/net')
            if (actual.st_dev, actual.st_ino) != (current.st_dev, current.st_ino):
                raise StartError('START_PEER_REJECTED')
            inode, populated = self.cgroup()
            fresh = self.observe()
            poll = select.poll(); poll.register(fd, select.POLLIN)
            if (not inode or not populated or fresh != observed or self.job() != job_path or poll.poll(0)):
                raise StartError('START_PEER_REJECTED')
            return observed['InvocationID'], inode
        finally:
            os.close(fd)

    def verify_running(self, state, helper, executable):
        start = state['start']
        if start['phase'] != 'START_ADMITTED':
            raise StartError('START_RECONCILIATION_REQUIRED')
        observed = self.observe()
        inode, populated = self.cgroup()
        if (observed['ActiveState'] != 'active' or observed['SubState'] != 'running'
                or observed['ControlPID'] != '0' or not re.fullmatch('[1-9][0-9]*', observed['MainPID'])
                or observed['InvocationID'] != start['invocation_id']
                or observed['ControlGroup'] != state['stop']['control_group']
                or inode != start['cgroup_inode'] or not populated or self.job() != '/'):
            raise StartError('START_RECONCILIATION_REQUIRED')
        if self.start_contract(observed, helper, executable) != start['contract_sha256']:
            raise StartError('START_BINDING_CHANGED')
        # Never infer loaded executable identity from the on-disk pathname alone.
        pid = int(observed['MainPID'])
        fd = os.pidfd_open(pid)
        try:
            with open('/proc/' + str(pid) + '/exe', 'rb') as running:
                actual = hashlib.file_digest(running, 'sha256').hexdigest()
            poll = select.poll(); poll.register(fd, select.POLLIN)
            if actual != executable or self.observe() != observed or poll.poll(0):
                raise StartError('START_EXECUTABLE_MISMATCH')
        finally:
            os.close(fd)

    def admit(self, guard, connection, state, helper, executable, expected):
        if (state['start']['phase'] != 'START_INTENT' or not state['start']['job_path']
                or guard.load() != state):
            raise StartError('START_RECONCILIATION_REQUIRED')
        connection.settimeout(5)
        protocol = b''
        while len(protocol) < 18:
            block = connection.recv(18 - len(protocol))
            if not block:
                break
            protocol += block
        if protocol != b'WAVEMESH_START_V1\n':
            raise StartError('START_PEER_REJECTED')
        invocation, inode = self.peer_identity(connection, state['start']['job_path'])
        if invocation == state['stop']['invocation_id']:
            raise StartError('START_PEER_REJECTED')
        if (self.boot_id() != state['stop']['boot_id']
                or self.start_contract(self.observe(), helper, executable) != state['start']['contract_sha256']):
            raise StartError('START_BINDING_CHANGED')
        verify_readback(PanelIsolation().observe(), expected)
        if self.peer_identity(connection, state['start']['job_path']) != (invocation, inode):
            raise StartError('START_PEER_REJECTED')
        admitted = {**state, 'start': {**state['start'], 'phase': 'START_ADMITTED',
                                      'invocation_id': invocation, 'cgroup_inode': inode}}
        guard.save(admitted)  # Consumed durably BEFORE the only success response.
        connection.sendall(b'OK\n')
        return admitted

    @contextmanager
    def started(self, guard, operation_id, generation, candidate_sha256, rollback_manifest_sha256,
                port, helper_sha256, executable_sha256, node_lock=None):
        if (sys.platform != 'linux' or os.geteuid() != 0 or guard.root != journal.DEFAULT_ROOT
                or not isinstance(executable_sha256, str) or not re.fullmatch(
                '[a-f0-9]{64}', executable_sha256)):
            raise StartError('START_SCOPE_INVALID')
        expected = bound_policy(operation_id, generation, candidate_sha256, rollback_manifest_sha256, port)
        with guard.installation_intent(operation_id, generation, candidate_sha256, rollback_manifest_sha256, node_lock):
            state = guard.load()
            if state['schema_version'] not in (4, 5) or self.boot_id() != state['stop']['boot_id']:
                raise StartError('START_STOP_PROOF_REQUIRED')
            verify_readback(PanelIsolation().observe(), expected)
            replay = state['schema_version'] == 5
            if replay:
                if state['start']['executable_sha256'] != executable_sha256:
                    raise StartError('START_BINDING_CHANGED')
                self.verify_running(state, helper_sha256, executable_sha256)
            else:
                observed = self.observe()
                self.verify_drained(observed, state['stop'])
                if self.contract(observed, helper_sha256) != state['stop']['contract_sha256'] or self.job() != '/':
                    raise StartError('START_BINDING_CHANGED')
                contract = self.start_contract(observed, helper_sha256, executable_sha256)
                endpoint = guard.root / 'start.sock'
                # A stale socket is unresolved state, never delete-and-retry.
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(str(endpoint))
                    os.chmod(endpoint, 0o600)
                    owned = endpoint.lstat()
                    try:
                        listener.listen(1)
                        listener.settimeout(20)
                        state = {**state, 'schema_version': 5, 'start': {
                            'phase': 'START_INTENT', 'job_path': '', 'invocation_id': '', 'cgroup_inode': 0,
                            'executable_sha256': executable_sha256, 'contract_sha256': contract}}
                        guard.save(state)
                        job = self.dispatch_start()
                        state = {**state, 'start': {**state['start'], 'job_path': job}}
                        guard.save(state)
                        connection, _ = listener.accept()
                        with connection:
                            state = self.admit(guard, connection, state, helper_sha256, executable_sha256, expected)
                        deadline = time.monotonic() + 20
                        while self.observe()['ActiveState'] == 'activating' and time.monotonic() < deadline:
                            time.sleep(0.05)
                        self.verify_running(state, helper_sha256, executable_sha256)
                    finally:
                        current = endpoint.lstat()
                        if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                            endpoint.unlink()
            verify_readback(PanelIsolation().observe(), expected)
            yield {'activation': 'RUNNING_BOUND_INVOCATION', 'local_admission': 'CLOSED',
                   'reconciliation_required': replay, 'commercial_access': 'NOT_PROVEN'}
