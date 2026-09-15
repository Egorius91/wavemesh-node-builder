"""Internal verified stop context. No CLI, restart, file replacement or release."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import sys

import panel_request_guard as journal
from panel_isolation import PanelIsolation, bound_policy, verify_readback

STARTUP_GUARD = Path('/usr/local/lib/wavemesh/lib/panel_request_guard.py')
CGROUP_ROOT = Path('/sys/fs/cgroup')
SETTINGS = {'LoadState': 'loaded', 'KillMode': 'control-group', 'SendSIGKILL': 'yes',
            'DynamicUser': 'no', 'RootDirectory': '', 'RootImage': '',
            'BindPaths': '', 'BindReadOnlyPaths': '', 'TemporaryFileSystem': ''}
VARIABLE = {'ActiveState', 'SubState', 'MainPID', 'ControlPID', 'ControlGroup', 'InvocationID',
            'User', 'NetworkNamespacePath'}
HOOKS = ('ExecStartPre', 'ExecStartPost', 'ExecStop', 'ExecStopPost', 'ExecCondition')
EXECUTION_ENVIRONMENT = {
    'PrivateNetwork': ('Service', 'b', False),
    'JoinsNamespaceOf': ('Unit', 'as', []),
    'MountImages': ('Service', 'a(ssba(ss))', []),
    'ExtensionImages': ('Service', 'a(sba(ss))', []),
    'ExtensionDirectories': ('Service', 'as', []),
}


class StopError(RuntimeError):
    pass


def parse_properties(raw):
    result = {}
    for line in raw.decode('utf-8').splitlines():
        key, sep, value = line.partition('=')
        if not sep or key in result or key not in SETTINGS.keys() | VARIABLE:
            raise StopError('STOP_UNIT_INVALID')
        result[key] = value
    if set(result) != SETTINGS.keys() | VARIABLE:
        raise StopError('STOP_UNIT_INVALID')
    return result


def verify_hooks(properties, helper):
    argv = ['/usr/bin/python3', '-I', '-B', str(helper), '--check-startup']
    if set(properties) != set(HOOKS):
        raise StopError('STOP_HOOK_INVALID')
    for name, value in properties.items():
        if (not isinstance(value, dict) or set(value) != {'type', 'data'}
                or value['type'] != 'a(sasbttttuii)' or not isinstance(value['data'], list)):
            raise StopError('STOP_HOOK_INVALID')
        entries = value['data']
        if name != 'ExecStartPre':
            if entries:
                raise StopError('STOP_HOOK_INVALID')
        elif (len(entries) != 1 or not isinstance(entries[0], list) or len(entries[0]) != 10
              or entries[0][:3] != ['/usr/bin/python3', argv, False]
              or type(entries[0][2]) is not bool
              or any(type(number) is not int for number in entries[0][3:])):
            raise StopError('STOP_HOOK_INVALID')


def verify_execution_environment(properties):
    if set(properties) != set(EXECUTION_ENVIRONMENT):
        raise StopError('STOP_EXECUTION_ENVIRONMENT_UNSUPPORTED')
    for name, (_, signature, expected) in EXECUTION_ENVIRONMENT.items():
        value = properties[name]
        if (not isinstance(value, dict) or set(value) != {'type', 'data'}
                or value['type'] != signature or type(value['data']) is not type(expected)
                or value['data'] != expected):
            raise StopError('STOP_EXECUTION_ENVIRONMENT_UNSUPPORTED')


class PanelStop:
    def run(self, args):
        try:
            result = subprocess.run(args, capture_output=True, timeout=30,
                                    env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        except (OSError, subprocess.TimeoutExpired):
            raise StopError('STOP_COMMAND_UNCERTAIN') from None
        if result.returncode or len(result.stdout) > 65536:
            raise StopError('STOP_COMMAND_UNCERTAIN')
        return result.stdout

    def observe(self):
        fields = ','.join(sorted(SETTINGS.keys() | VARIABLE))
        return parse_properties(self.run(['/usr/bin/systemctl', 'show', journal.PANEL_UNIT, '--property=' + fields]))

    def boot_id(self):
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def property(self, name, interface='Service'):
        encoded = ''.join(c if c.isascii() and c.isalnum() else '_' + format(ord(c), '02x')
                          for c in journal.PANEL_UNIT)
        raw = self.run(['/usr/bin/busctl', '--system', '--json=short', 'get-property',
                        'org.freedesktop.systemd1', '/org/freedesktop/systemd1/unit/' + encoded,
                        'org.freedesktop.systemd1.' + interface, name])
        return json.loads(raw, object_pairs_hook=journal.unique_object)

    def verify_main_namespace(self, observed):
        if not re.fullmatch('[1-9][0-9]*', observed['MainPID']):
            raise StopError('STOP_PROCESS_IDENTITY_UNPROVEN')
        pid = int(observed['MainPID'])
        fd = os.pidfd_open(pid)
        try:
            # A pidfd becoming readable detects death/reuse while the numeric
            # /proc path is inspected. Never signal a numeric PID here.
            actual = os.stat('/proc/' + str(pid) + '/ns/net')
            current = os.stat('/proc/self/ns/net')
            if (actual.st_dev, actual.st_ino) != (current.st_dev, current.st_ino):
                raise StopError('STOP_NETWORK_NAMESPACE_MISMATCH')
            fresh = self.observe()
            if any(fresh[key] != observed[key] for key in
                   ('MainPID', 'InvocationID', 'ControlGroup', 'ActiveState', 'SubState')):
                raise StopError('STOP_PROCESS_IDENTITY_UNPROVEN')
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            if poll.poll(0):
                raise StopError('STOP_PROCESS_IDENTITY_UNPROVEN')
        finally:
            os.close(fd)

    def contract(self, observed, helper_sha256):
        if any(observed[k] != v for k, v in SETTINGS.items()) or observed['User'] not in ('', 'root', '0'):
            raise StopError('STOP_UNIT_UNSUPPORTED')
        environment = {name: self.property(name, interface)
                       for name, (interface, _, _) in EXECUTION_ENVIRONMENT.items()}
        verify_execution_environment(environment)
        # The nft readback must inspect the namespace used by the service.
        namespace = observed['NetworkNamespacePath'] or '/proc/1/ns/net'
        configured, current = os.stat(namespace), os.stat('/proc/self/ns/net')
        if (configured.st_dev, configured.st_ino) != (current.st_dev, current.st_ino):
            raise StopError('STOP_NETWORK_NAMESPACE_MISMATCH')
        if observed['ActiveState'] == 'active':
            self.verify_main_namespace(observed)
        for path in STARTUP_GUARD.parents:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise StopError('STOP_HELPER_UNSAFE')
        fd = os.open(STARTUP_GUARD, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                    or info.st_mode & 0o022 or info.st_size > 262144):
                raise StopError('STOP_HELPER_UNSAFE')
            digest = hashlib.sha256(os.read(fd, 262145)).hexdigest()
        finally:
            os.close(fd)
        if digest != helper_sha256:
            raise StopError('STOP_HELPER_MISMATCH')
        hooks = {name: self.property(name) for name in HOOKS}
        verify_hooks(hooks, STARTUP_GUARD)
        # Runtime timestamps/PIDs inside ExecStartPre are observations, not
        # configuration. Hash only the strictly verified command contract.
        identity = {k: observed[k] for k in SETTINGS.keys() | {'User', 'NetworkNamespacePath'}}
        identity['helper_sha256'] = digest
        identity['execution_environment'] = environment
        identity['network_namespace'] = [current.st_dev, current.st_ino]
        identity['startup_argv'] = hooks['ExecStartPre']['data'][0][:3]
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def cgroup(self):
        # Kernel-owned cgroup-v2 hierarchy; no caller-supplied path.
        if not (CGROUP_ROOT / 'cgroup.controllers').is_file():
            raise StopError('STOP_CGROUP_UNSUPPORTED')
        directory = CGROUP_ROOT / 'system.slice' / journal.PANEL_UNIT
        (CGROUP_ROOT / 'system.slice').stat()
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return 0, False
        if not stat.S_ISDIR(info.st_mode):
            raise StopError('STOP_CGROUP_INVALID')
        values = [line.split() for line in (directory / 'cgroup.events').read_text().splitlines()
                  if line.split()[:1] == ['populated']]
        if values not in ([['populated', '0']], [['populated', '1']]):
            raise StopError('STOP_CGROUP_INVALID')
        return info.st_ino, values[0][1] == '1'

    def dispatch_stop(self):
        self.run(['/usr/bin/systemctl', 'stop', journal.PANEL_UNIT])

    def verify_drained(self, observed, intent):
        if (observed['ActiveState'] != 'inactive' or observed['SubState'] != 'dead'
                or observed['MainPID'] != '0' or observed['ControlPID'] != '0'
                or observed['ControlGroup'] not in ('', intent['control_group'])
                or observed['InvocationID'] not in ('', intent['invocation_id'])):
            raise StopError('STOP_RECONCILIATION_REQUIRED')
        inode, populated = self.cgroup()
        if populated or (inode and inode != intent['cgroup_inode']):
            raise StopError('STOP_DESCENDANTS_UNPROVEN')

    @contextmanager
    def stopped(self, guard, operation_id, generation, candidate_sha256, rollback_manifest_sha256,
                port, helper_sha256, node_lock=None):
        if (sys.platform != 'linux' or os.geteuid() != 0 or guard.root != journal.DEFAULT_ROOT
                or not isinstance(helper_sha256, str) or not re.fullmatch('[a-f0-9]{64}', helper_sha256)):
            raise StopError('STOP_SCOPE_INVALID')
        expected = bound_policy(operation_id, generation, candidate_sha256, rollback_manifest_sha256, port)
        isolation = PanelIsolation()
        with guard.installation_intent(operation_id, generation, candidate_sha256, rollback_manifest_sha256, node_lock):
            verify_readback(isolation.observe(), expected)
            observed = self.observe()
            contract = self.contract(observed, helper_sha256)
            boot = self.boot_id()
            state = guard.load()
            replay = state['schema_version'] == 4
            if replay:
                intent = state['stop']
                if intent['boot_id'] != boot or intent['contract_sha256'] != contract:
                    raise StopError('STOP_BINDING_CHANGED')
            else:
                group = '/system.slice/' + journal.PANEL_UNIT
                active = observed['ActiveState'] == 'active' and observed['SubState'] == 'running'
                inactive = observed['ActiveState'] == 'inactive' and observed['SubState'] == 'dead'
                if (not (active or inactive) or observed['ControlPID'] != '0'
                        or observed['ControlGroup'] not in ('', group)
                        or (active and (observed['ControlGroup'] != group
                            or not re.fullmatch('[1-9][0-9]*', observed['MainPID'])
                            or not re.fullmatch('[a-f0-9]{32}', observed['InvocationID'])))
                        or (inactive and observed['MainPID'] != '0')):
                    raise StopError('STOP_STATE_UNSUPPORTED')
                inode, populated = self.cgroup()
                if (active and (not inode or not populated)) or (inactive and populated):
                    raise StopError('STOP_CGROUP_INVALID')
                intent = {'phase': 'STOP_INTENT', 'unit': journal.PANEL_UNIT, 'boot_id': boot,
                          'invocation_id': observed['InvocationID'], 'control_group': group,
                          'cgroup_inode': inode, 'contract_sha256': contract}
                guard.validate_stop(intent)
                guard.save({**state, 'schema_version': 4, 'stop': intent})
                if active:
                    self.dispatch_stop()
            observed = self.observe()
            if self.boot_id() != boot or self.contract(observed, helper_sha256) != contract:
                raise StopError('STOP_BINDING_CHANGED')
            self.verify_drained(observed, intent)
            verify_readback(isolation.observe(), expected)
            yield {'backend_cgroup': 'DRAINED', 'scope': 'CURRENT_LOCKED_CONTEXT',
                   'reconciliation_required': replay, 'quiescence': 'NOT_PROVEN'}
