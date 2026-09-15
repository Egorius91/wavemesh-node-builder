# Terminal maintenance recovery

`PanelMaintenanceRecovery.recovered()` is an internal, explicit recovery API
for an already admitted maintenance process that has stopped or failed. It
requires a caller-supplied canonical attempt UUID and the expected previous
systemd invocation. It never stops an active service or edits configuration,
panel files or the database. Starting the backend may run its normal database
initialization; this protocol does not claim zero database writes or migration
rollback safety.

The controller retains the Node and request-journal locks, verifies the existing
candidate/snapshot/replacement receipt and exact maintenance start contract,
and requires the same boot. Terminal proof requires inactive/dead or
failed/failed, zero MainPID and ControlPID, no systemd job, and an empty cgroup
that is absent or still has the previous admitted inode. A different invocation,
live descendants, environment drift or a partially completed start prevents
dispatch. These observations are repeated before durable intent.

Journal v8 retains replacement evidence and a bounded list of recovery attempts.
Each entry records the attempt ID, previous admitted start and terminal state.
It preserves up to 16 attempts in a 32 KiB journal; the limit fails closed and
does not discard history or implicitly begin another installation generation.
The new START_INTENT and history are saved together before the single StartUnit
call. Admission still checks the kernel peer, exact job and new invocation,
persists START_ADMITTED before acknowledgment and verifies the running binary
and immutable maintenance environment.

Repeating the latest attempt with the same previous invocation is readback only:
it can confirm an already running bound process, but never dispatch or grant
again. Older attempt IDs, conflicting previous invocation and a new ID while the
current process is running are rejected. A new attempt is possible only after
the current admitted process is terminal. A START_INTENT of uncertain outcome,
a stale broker socket after controller death, reboot, or exhausted history
requires a separate reconciliation procedure; changing the attempt ID cannot
bypass uncertainty.

The existing start/stop/rollback/cancellation paths do not downgrade v8. Writer
admission and commercial runtime remain closed. Current SaaS ownership and
subscription/revocation reconciliation is still required before VPN restoration.
The systemd manager and privileged host administrator remain trusted.

CI adds `maintenance-recovery-smoke.json` to the five existing reports. In a
private network with the actual packaged panel it recovers a cleanly stopped
maintenance process, then a SIGKILL-failed process. Both attempts lose their
result and are reconciled without a second StartUnit/grant. It rejects active
restarts and stale IDs, checks unchanged client identities, denied real VLESS,
and persistent Agent/CLI exclusion after lock release. Root unit tests cover
terminal drift, pending jobs, descendants, strict history, lost dispatch and
controller death.
