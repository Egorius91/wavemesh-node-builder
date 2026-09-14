# Panel database rollback admission

The managed transaction rollback previously continued after `systemctl stop
x-ui` failed and copied one SQLite file over the destination without handling
its WAL. It also restored configuration before attempting to stop the panel.
That could modify a live database or let an old WAL conflict with restored data.

For transactions containing a panel DB snapshot, rollback now validates the
snapshot/path pair, requires a successful stop, then checks systemd's loaded,
inactive/dead state with no main/control PID and control-group termination with
SIGKILL enabled. A reported nonempty control group additionally requires readable
cgroup-v2 events proving recursive population zero. Missing/ambiguous/unsupported
observations fail closed. No configuration or DB restore precedes this admission.

`panel_restore.py` restores using the [SQLite backup API](https://www.sqlite.org/backup.html),
which replaces destination contents through SQLite's own transaction and journal
protocol. It validates the closed backup, rejects unsafe paths/links and backup
sidecars, uses FULL synchronous writes and bounded backup lock waiting, checks
destination integrity and lets SQLite truncate its WAL. It never unlinks WAL
or journal files manually. Raw SQLite messages and contents are not logged.

Stop, observation, DB restoration or pre-start configuration failure retains
`rollback_failed` and the original backups; it does not start the panel or
continue to later nginx/subscription restoration. Failed start/readiness also
stops subsequent steps. A restore may already have committed when a subsequent
checkpoint or permission step fails: keep the service stopped and reconcile;
do not assume an error means no DB change. An explicit recovery continues to
use the same snapshot, after the shared request uncertainty guard admits it.

Tests exercise SQLite WAL left by a dead writer, lock contention, corrupt
snapshots, interrupted copying and process death with a partially copied DB.
Bash tests verify stop failure and false-success observations prevent later
effects. Existing successful and automatic transaction rollback remain covered.

This is a correction to the existing CLI rollback, **not a complete panel
deployment transaction or exclusive-writer protocol**. The Node lock excludes
cooperating local mutations only; systemd observations do not persistently mask
service activation or exclude manual/remote writers. Stopping x-ui also stops
the Xray process in its service group. Deployment still needs an ingress/writer
maintenance barrier, durable stop/restart recovery, binary/DB/config provenance,
compatible rollback and staging HTTP/VPN acceptance. The uncertainty journal is
not cleared or used as proof that a remote request has drained.
