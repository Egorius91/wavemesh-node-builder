# Durable local maintenance admission

This is one prerequisite for safe Entry/Exit maintenance. It closes new
cooperating local writers across process death and restart. It does not stop
3X-UI/Xray, remove clients, prove remote-writer isolation, or establish drained
runtime. SaaS remains the business authority; these are local operator commands,
not additional remotely executable Agent commands.

## Operator interface

Run the installed CLI as the same trusted root operator as the Node Agent:

```text
wavemesh maintenance status
wavemesh maintenance prepare OPERATION_UUID GENERATION
wavemesh maintenance cancel OPERATION_UUID GENERATION
```

Use a canonical lowercase UUID and an integer generation, starting at 1. A new
hold requires the previous hold to be cancelled and the next consecutive
generation. Identity is the tuple (operation UUID, generation); a reused UUID
does not bypass the generation check. Generation is bounded to 2147483647 and
never wraps. Stale, skipped or conflicting generations are rejected.

Prepare/cancel acquire `/run/lock/wavemesh-node.lock` and then the protected panel
journal lock, both nonblocking. A current Node mutation or panel HTTP dispatch
causes the command to fail without claiming a hold. After prepare returns
success, a new cooperating writer cannot enter. Repeating the same prepare
returns the existing hold; repeating it after cancellation returns CANCELLED and
does not close admission again. No timer or receipt expiry releases the hold.

Status reports `local_admission` (CLOSED or NOT_HELD), operation metadata,
`request_pending`, and always `quiescence: NOT_PROVEN`. NOT_HELD does not prove
that writes can proceed: an uncertain request or another lock may still block
them. Status takes the journal lock and may initialize its directory/lock when
absent; it never clears existing state. Output contains no request bodies,
credentials, client identifiers or subscription URLs.

Cancellation only undoes this local admission hold when no unresolved panel
request remains. It performs no service, DB, credentials or topology changes.
**Do not use it as a release after an external maintenance action.** A future
maintenance actuator must persist a separate non-cancellable phase before its
first external change, then reconcile and prove recovery before reopening. This
component exposes no such external action or quiescence receipt.

## Internal installation intent boundary

`PanelRequestGuard.installation_intent(operation_id, generation,
candidate_sha256, rollback_manifest_sha256)` is a lock-holding Python context for
the future verified installation actuator. It is not a CLI or remote command,
and existing installation scripts do not invoke it. Both digests must be exact
lowercase SHA256 strings. They bind the caller's intended immutable inputs;
the guard does not verify the artifacts, backup contents, freshness, provenance,
writer exclusion or backend drain on the caller's behalf.

The context acquires Node then journal lock. It requires the exact HELD tuple
and no unresolved panel request, retains accepted request history, and atomically
upgrades to schema v3 with an `INSTALL_INTENT` record before yielding to any
external work. File and directory fsync must succeed first. The installed Agent,
CLI and source rollback continue to reject mutation. Old v1/v2 readers reject
the unknown schema rather than treating it as an ordinary cancellable hold.

Once v3 exists, ordinary prepare/cancel are rejected, including after process
death or failure before the first external effect. Status reports the bound
installation and still says `quiescence: NOT_PROVEN`. No timeout, context exit,
exception or source reinstall releases this state. Re-entering with the exact
same tuple and digests returns `reconciliation_required: true`; different input
is rejected. That flag prohibits interpreting a replay as permission to repeat
external work blindly. A lost response or post-replace fsync error may leave a
committed intent even though the first caller never entered its external body.

There is no installation release/recovery transition yet. A subsequent actuator
must implement evidence-backed recovery and reopening, retain both locks during
each operation and revalidate its immutable inputs before effects. This internal
primitive alone must not be used to begin a live installation: it does not stop,
drain, replace, restore or start 3X-UI/Xray. Those external steps and their
reconciliation remain the next required implementation.

## Persistence and uncertainty

The canonical journal remains
`/var/lib/wavemesh-agent/panel-requests/state.json`. First prepare atomically
upgrades its schema to v2, retaining the previous request record. A cancelled
hold remains in the envelope during all subsequent HTTP requests so an old
prepare/cancel cannot change a newer generation. Old v1 guard readers reject v2
instead of silently ignoring maintenance. Existing v1 journals remain readable.

File fsync, atomic replacement and directory fsync precede a successful result.
If the result is lost or persistence fails, inspect status and reconcile the
same tuple; never delete the journal or generate a new operation to bypass the
failure. In particular, a failed directory fsync after replacement can leave
either prepare or cancellation visible without a successful receipt. No claim
is made that cancellation always leaves the hold active on an I/O error.

An unresolved DISPATCH_INTENT can be enclosed by a hold but is never cleared by
prepare/cancel. Cancellation is rejected until that uncertainty is reconciled
by a separately designed procedure. This change adds no reset/retry escape hatch.

## Covered admission paths and limitations

- Agent access mutations check the hold while owning the shared Node lock,
  before configuration, fencing, panel requests or access state effects.
- Guarded Python and installed Bash panel writes reject the hold. Panel GETs
  and already allow-listed read POSTs remain available for diagnosis.
- CLI topology/transaction mutation admission and transaction rollback reject
  the hold. `repair --nginx` and `repair --ssl` now use Node admission too.
- Agent source rollback checks the hold before source/service effects and
  rejects backups without declared v2 support after an upgrade, including after
  cancellation. A compatible rollback preserves the journal. The protocol
  marker is a compatibility declaration for trusted backup source, not a
  signature or independent source integrity proof.
- CLI reinstallation preserves the journal. It is not a source-upgrade or
  service-stop transaction; deployment must coordinate separately.

This is cooperative local exclusion, not a boundary against root, manual
systemctl/curl, remote SaaS/3X-UI writers, cron inside the panel, or requests
already accepted by a backend. It neither cancels such work nor fences runtime
after a caller timeout. Existing runtime findings collection also uses Node
admission and may be unavailable during a hold; maintenance status and direct
guarded panel reads remain available. A future drain proof must not depend on
that collector running while held.

Do not activate commercial replacement using this component alone. Remote
credential/listener isolation, reconciled pending operations, backend/Xray
drain, maintenance-aware artifact installation and restoration proof remain
prerequisites. SaaS operation binding, mTLS identity and fleet placement are
separate contracts, not inferred from a local UUID.

## Verification

Linux tests exercise real files, flock, fsync and independent processes:
`test_local_maintenance.py` covers stale commands, replay, writer races, killed
dispatch, lost results, schema corruption and storage failures.
`test_installed_panel_guard.py` uses the actual CLI installer and a disposable
HTTP panel to prove blocked requests and repair commands have no external
effects. Only fixed runtime lock paths are relocated in those fixtures; no
production path-bypass argument is added. `node_agent_rollback_latest.sh`
exercises installed rollback, cancellation history and compatibility rejection.
These are source/CI checks, not staging, reboot or real VPN acceptance.

`test_installation_intent.py` additionally covers exact binding, unchanged
accepted/pending history, lock ownership before effects, killed processes,
conflicting replay, file/replace/directory sync failures and malformed v3 state.
Installed CLI reinstallation and actual rollback smoke retain the v3 hold and
reject cancellation/writes/source restoration before effects.
