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
