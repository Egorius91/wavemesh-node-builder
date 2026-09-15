"""Explicit same-boot recovery of a terminal admitted maintenance invocation."""
from contextlib import contextmanager
import os
import re
import sys

import panel_request_guard as journal
from panel_isolation import PanelIsolation, bound_policy, verify_readback
from panel_maintenance_start import PanelMaintenanceStart
from panel_start import StartError


class PanelMaintenanceRecovery(PanelMaintenanceStart):
    def started(self, *args, **kwargs):
        raise StartError('RECOVERY_ATTEMPT_REQUIRED')

    def terminal(self, state, helper, executable):
        previous = state['start']
        if previous['phase'] != 'START_ADMITTED':
            raise StartError('RECOVERY_ADMISSION_REQUIRED')
        observed = self.observe()
        if (observed['ActiveState'], observed['SubState']) not in (('inactive', 'dead'), ('failed', 'failed')):
            raise StartError('RECOVERY_TERMINAL_REQUIRED')
        if (observed['MainPID'] != '0' or observed['ControlPID'] != '0'
                or observed['ControlGroup'] not in ('', state['stop']['control_group'])
                or observed['InvocationID'] not in ('', previous['invocation_id'])
                or self.job() != '/'):
            raise StartError('RECOVERY_TERMINAL_REQUIRED')
        inode, populated = self.cgroup()
        if populated or inode not in (0, previous['cgroup_inode']):
            raise StartError('RECOVERY_DESCENDANTS_UNPROVEN')
        if self.start_contract(observed, helper, executable) != previous['contract_sha256']:
            raise StartError('RECOVERY_BINDING_CHANGED')
        if self.observe() != observed or self.cgroup() != (inode, False) or self.job() != '/':
            raise StartError('RECOVERY_TERMINAL_CHANGED')
        return observed['ActiveState'], inode

    def peer_identity(self, connection, job_path):
        invocation, inode = super().peer_identity(connection, job_path)
        # Every recovery must be a new invocation, never an old accepted peer.
        if invocation in self._prior_invocations:
            raise StartError('RECOVERY_OLD_INVOCATION')
        return invocation, inode

    @contextmanager
    def recovered(self, guard, operation_id, generation, candidate_sha256, rollback_manifest_sha256,
                  port, helper_sha256, executable_sha256, attempt_id, previous_invocation, node_lock=None):
        journal.validate_hold_identity(attempt_id, 1)
        if (sys.platform != 'linux' or os.geteuid() != 0 or guard.root != journal.DEFAULT_ROOT
                or not isinstance(previous_invocation, str) or not re.fullmatch('[a-f0-9]{32}', previous_invocation)
                or not isinstance(executable_sha256, str) or not re.fullmatch('[a-f0-9]{64}', executable_sha256)):
            raise StartError('RECOVERY_SCOPE_INVALID')
        expected = bound_policy(operation_id, generation, candidate_sha256, rollback_manifest_sha256, port)
        with guard.installation_intent(operation_id, generation, candidate_sha256, rollback_manifest_sha256, node_lock):
            state = guard.load()
            if state['schema_version'] not in (7, 8) or self.boot_id() != state['stop']['boot_id']:
                raise StartError('RECOVERY_ADMISSION_REQUIRED')
            verify_readback(PanelIsolation().observe(), expected)
            self.verify_source(guard, state, executable_sha256)
            rows = state.get('recoveries', [])
            matching = [row for row in rows if row['attempt_id'] == attempt_id]
            if matching:
                if matching[0] != rows[-1] or matching[0]['previous_start']['invocation_id'] != previous_invocation:
                    raise StartError('RECOVERY_ATTEMPT_CONFLICT')
                self.verify_running(state, helper_sha256, executable_sha256)
                replay = True
            else:
                if len(rows) >= journal.MAX_RECOVERIES:
                    raise StartError('RECOVERY_HISTORY_LIMIT')
                if state['start']['invocation_id'] != previous_invocation:
                    raise StartError('RECOVERY_ATTEMPT_CONFLICT')
                terminal_state, inode = self.terminal(state, helper_sha256, executable_sha256)
                record = {'attempt_id': attempt_id, 'previous_start': state['start'],
                          'terminal_state': terminal_state, 'terminal_cgroup_inode': inode}
                rows = [*rows, record]
                self._prior_invocations = {row['previous_start']['invocation_id'] for row in rows}
                pending = {**state, 'recoveries': rows}
                # Both the terminal observation and source are verified again
                # before the one durable intent/new StartUnit transition.
                self.verify_source(guard, state, executable_sha256)
                if self.boot_id() != state['stop']['boot_id'] or self.terminal(state, helper_sha256, executable_sha256) != (terminal_state, inode):
                    raise StartError('RECOVERY_TERMINAL_CHANGED')
                verify_readback(PanelIsolation().observe(), expected)
                state = self.dispatch_once(guard, pending, helper_sha256, executable_sha256,
                                           expected, state['start']['contract_sha256'], 8)
                replay = False
            verify_readback(PanelIsolation().observe(), expected)
            yield {'activation': 'MAINTENANCE_BOUND_INVOCATION', 'local_admission': 'CLOSED',
                   'runtime_activation': 'DENIED', 'reconciliation_required': replay,
                   'authority_reconciliation': 'REQUIRED'}
