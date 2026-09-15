"""Atomic panel tree exchange under a stopped backend; no DB restore/start/release."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

import panel_backup as storage
import panel_candidate as candidate
import panel_request_guard as journal
from panel_isolation import PanelIsolation, bound_policy, verify_readback
from panel_stop import PanelStop


class ReplacementError(RuntimeError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def tree_digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def location(operation_id, generation):
    journal.validate_hold_identity(operation_id, generation)
    return candidate.PANEL_HOME.parent / ('.wavemesh-replace-' + operation_id + '-' + str(generation))


def describe(path):
    storage.directory(path)
    before = path.lstat()
    inventory = storage.tree(path)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_mode, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns):
        raise ReplacementError('REPLACEMENT_TREE_CHANGED')
    return {'device': before.st_dev, 'inode': before.st_ino, 'mode': stat.S_IMODE(before.st_mode),
            'tree_sha256': tree_digest(inventory)}


def validate_receipt(value, identity, old_inventory, new_inventory):
    if (not isinstance(value, dict) or set(value) != {'schema', 'identity', 'original', 'candidate'}
            or type(value['schema']) is not int or value['schema'] != 1
            or encoded(value['identity']) != encoded(identity)):
        raise ReplacementError('REPLACEMENT_RECEIPT_INVALID')
    for key, inventory in (('original', old_inventory), ('candidate', new_inventory)):
        image = value[key]
        if (not isinstance(image, dict) or set(image) != {'device', 'inode', 'mode', 'tree_sha256'}
                or type(image['device']) is not int or image['device'] < 0
                or type(image['inode']) is not int or image['inode'] <= 0
                or type(image['mode']) is not int or image['mode'] & ~0o777 or image['mode'] & 0o022
                or image['tree_sha256'] != tree_digest(inventory)):
            raise ReplacementError('REPLACEMENT_RECEIPT_INVALID')
    if (value['original']['device'] != value['candidate']['device']
            or value['original']['inode'] == value['candidate']['inode']):
        raise ReplacementError('REPLACEMENT_FILESYSTEM_UNSUPPORTED')


def exchange(left, right):
    """Single fixed-flags syscall. No two-rename fallback on unsupported systems."""
    try:
        call = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise ReplacementError('REPLACEMENT_EXCHANGE_UNSUPPORTED') from None
    call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    call.restype = ctypes.c_int
    fds = []
    try:
        for path in (left.parent, right.parent):
            storage.directory(path)
            fds.append(os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        if call(fds[0], os.fsencode(left.name), fds[1], os.fsencode(right.name), 2) != 0:
            # No automatic retry even when errno would normally imply no effect.
            raise ReplacementError('REPLACEMENT_EXCHANGE_UNCERTAIN')
    finally:
        for fd in fds:
            os.close(fd)


class PanelReplacement:
    def stopped(self, state, helper, policy):
        controller = PanelStop()
        if controller.boot_id() != state['stop']['boot_id']:
            raise ReplacementError('REPLACEMENT_BOOT_CHANGED')
        observed = controller.observe()
        if controller.contract(observed, helper) != state['stop']['contract_sha256']:
            raise ReplacementError('REPLACEMENT_STOP_BINDING_CHANGED')
        controller.verify_drained(observed, state['stop'])
        verify_readback(PanelIsolation().observe(), policy)

    def position(self, root, receipt):
        live, slot = describe(candidate.PANEL_HOME), describe(root / 'slot')
        if live == receipt['original'] and slot == receipt['candidate']:
            return 'original'
        if live == receipt['candidate'] and slot == receipt['original']:
            return 'candidate'
        raise ReplacementError('REPLACEMENT_ORIENTATION_UNPROVEN')

    def save_phase(self, guard, state, phase, receipt_sha256=None):
        replacement = {**state['replacement'], 'phase': phase}
        if receipt_sha256 is not None:
            replacement['receipt_sha256'] = receipt_sha256
        value = {**state, 'replacement': replacement}
        guard.validate_replacement(replacement)
        guard.save(value)
        return value

    def activation_tree(self, source, target, members):
        target.mkdir(mode=0o700)
        storage.tree(source, target)
        # These are validated static artifact modes. The copy stays below a
        # private parent until the exchange; the immutable prepared tree remains.
        for name, row in sorted(members.items(), reverse=True):
            path = target if name == 'x-ui' else target / name.removeprefix('x-ui/')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | (os.O_DIRECTORY if row['kind'] == 'directory' else 0))
            try:
                os.fchmod(fd, row['mode'])
                os.fsync(fd)
            finally:
                os.close(fd)
        storage.sync_dir(target.parent)

    def run(self, action, guard, operation_id, generation, candidate_sha256, rollback_sha256,
            manifest_sha256, head, port, helper_sha256, node_lock=None):
        if (action not in ('replace', 'rollback') or sys.platform != 'linux' or os.geteuid() != 0
                or guard.root != journal.DEFAULT_ROOT or candidate.PANEL_HOME != storage.PANEL_HOME):
            raise ReplacementError('REPLACEMENT_SCOPE_INVALID')
        root = location(operation_id, generation)
        policy = bound_policy(operation_id, generation, candidate_sha256, rollback_sha256, port)
        expected_installation = {'phase': 'INSTALL_INTENT', 'candidate_sha256': candidate_sha256,
                                 'rollback_manifest_sha256': rollback_sha256}
        journal.PanelRequestGuard.validate_installation(expected_installation)
        with journal.maintenance_node_lock(node_lock or Path('/run/lock/wavemesh-node.lock')), guard.locked():
            storage.directory(guard.root, private=True)
            state = guard.load()
            if (not state or state['schema_version'] not in (4, 6)
                    or guard.hold_state(state) != {'operation_id': operation_id, 'generation': generation, 'phase': 'HELD'}
                    or state['installation'] != expected_installation):
                raise ReplacementError('REPLACEMENT_STOP_REQUIRED')
            self.stopped(state, helper_sha256, policy)  # Observe only: never stop/start a service here.
            prepared = candidate.verify_locked(guard, operation_id, generation, manifest_sha256, head)
            if prepared['candidate_sha256'] != candidate_sha256:
                raise ReplacementError('REPLACEMENT_CANDIDATE_CHANGED')
            storage.verify_locked(guard, operation_id, generation, candidate_sha256, rollback_sha256)
            raw, _ = storage.read_file(storage.location(guard, operation_id, generation) / 'manifest.json', 2*1024*1024)
            old_inventory = json.loads(raw, object_pairs_hook=journal.unique_object)['panel']
            raw, _ = storage.read_file(candidate.location(operation_id, generation) / 'manifest.json', 1024*1024)
            members = candidate.manifest(raw, manifest_sha256, head)['members']
            new_inventory = {name.removeprefix('x-ui/'): row for name, row in members.items() if name != 'x-ui'}
            identity = [operation_id, generation, candidate_sha256, rollback_sha256, manifest_sha256, head,
                        state['stop']['boot_id'], state['stop']['contract_sha256']]
            initial = state['schema_version'] == 4
            if initial:
                if action != 'replace' or root.exists() or root.is_symlink():
                    raise ReplacementError('REPLACEMENT_RECONCILIATION_REQUIRED')
                if storage.tree(candidate.PANEL_HOME) != old_inventory:
                    raise ReplacementError('REPLACEMENT_BASELINE_CHANGED')
                original = describe(candidate.PANEL_HOME)
                if original['device'] != candidate.PANEL_HOME.parent.stat().st_dev:
                    raise ReplacementError('REPLACEMENT_FILESYSTEM_UNSUPPORTED')
                # Even a failed private activation-copy preparation must not be
                # bypassed by the older v4 recovery-start path.
                state = {**state, 'schema_version': 6, 'replacement': {
                    'phase': 'PREPARE_INTENT', 'manifest_sha256': manifest_sha256, 'receipt_sha256': ''}}
                guard.save(state)
                root.mkdir(mode=0o700)
                storage.sync_dir(root.parent)
                self.activation_tree(prepared['prepared_home'], root / 'slot', members)
                receipt = {'schema': 1, 'identity': identity, 'original': original,
                           'candidate': describe(root / 'slot')}
                validate_receipt(receipt, identity, old_inventory, new_inventory)
                if self.position(root, receipt) != 'original':
                    raise ReplacementError('REPLACEMENT_ORIENTATION_UNPROVEN')
                raw = encoded(receipt)
                storage.write_file(root / 'receipt.json', raw)
                storage.sync_dir(root)
                state = self.save_phase(guard, state, 'REPLACE_INTENT', hashlib.sha256(raw).hexdigest())
            else:
                if (state['replacement']['manifest_sha256'] != manifest_sha256
                        or state['replacement']['phase'] == 'PREPARE_INTENT'):
                    raise ReplacementError('REPLACEMENT_RECONCILIATION_REQUIRED')
                storage.directory(root, private=True)
                if {path.name for path in root.iterdir()} != {'receipt.json', 'slot'}:
                    raise ReplacementError('REPLACEMENT_RECONCILIATION_REQUIRED')
                raw, mode = storage.read_file(root / 'receipt.json', 4096)
                if mode != 0o600 or hashlib.sha256(raw).hexdigest() != state['replacement']['receipt_sha256']:
                    raise ReplacementError('REPLACEMENT_RECEIPT_CHANGED')
                receipt = json.loads(raw, object_pairs_hook=journal.unique_object)
                validate_receipt(receipt, identity, old_inventory, new_inventory)
            phase = state['replacement']['phase']
            position = self.position(root, receipt)
            exchanged = False
            if action == 'replace':
                if phase not in ('REPLACE_INTENT', 'REPLACED'):
                    raise ReplacementError('REPLACEMENT_RECONCILIATION_REQUIRED')
                if initial:
                    self.stopped(state, helper_sha256, policy)
                    exchange(candidate.PANEL_HOME, root / 'slot')
                    exchanged = True
                elif position != 'candidate':
                    # A failed/lost dispatch before the syscall is not permission
                    # to repeat it. Explicit rollback can accept the original pair.
                    raise ReplacementError('REPLACEMENT_RECONCILIATION_REQUIRED')
                target, final = 'candidate', 'REPLACED'
            else:
                if phase == 'ROLLED_BACK':
                    if position != 'original':
                        raise ReplacementError('REPLACEMENT_ORIENTATION_UNPROVEN')
                elif phase == 'ROLLBACK_INTENT':
                    if position != 'original':
                        raise ReplacementError('REPLACEMENT_RECONCILIATION_REQUIRED')
                elif phase in ('REPLACE_INTENT', 'REPLACED'):
                    if phase == 'REPLACED' and position != 'candidate':
                        raise ReplacementError('REPLACEMENT_ORIENTATION_UNPROVEN')
                    state = self.save_phase(guard, state, 'ROLLBACK_INTENT')
                    if position == 'candidate':
                        self.stopped(state, helper_sha256, policy)
                        exchange(candidate.PANEL_HOME, root / 'slot')
                        exchanged = True
                else:
                    raise ReplacementError('REPLACEMENT_RECONCILIATION_REQUIRED')
                target, final = 'original', 'ROLLED_BACK'
            storage.sync_dir(root)
            storage.sync_dir(root.parent)
            if self.position(root, receipt) != target:
                raise ReplacementError('REPLACEMENT_ORIENTATION_UNPROVEN')
            self.stopped(state, helper_sha256, policy)
            if state['replacement']['phase'] != final:
                state = self.save_phase(guard, state, final)
            return {'files': final, 'backend': 'STOPPED', 'local_admission': 'CLOSED',
                    'reconciliation_required': not exchanged, 'startup': 'DENIED', 'database': 'UNCHANGED_BY_THIS_OPERATION'}
