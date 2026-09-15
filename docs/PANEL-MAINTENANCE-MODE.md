# Pinned backend maintenance mode

The pinned panel accepts `WAVEMESH_PANEL_MODE=maintenance` at process start.
It captures the value before service environment files or panel settings load.
The process cannot switch to normal mode through HTTP, a settings change, an
environment reload or SIGHUP. An unknown nonempty value fails startup with a
fixed error. The default empty value preserves existing normal operation.

Maintenance admits exactly two GET routes under the configured panel base path:
`panel/api/wavemesh/maintenance` and `panel/api/clients/list`. Both retain existing
API authentication. The first returns protocol `wavemesh-maintenance-v1` and the
selected maintenance boolean; the second reads existing client attachments and
traffic records. All other HTTP routes, including legacy handlers, restart,
settings, import, WebSocket and login, are denied before handler execution.
Use an already provisioned API credential; this mode cannot create one.

No traffic writer, scheduled jobs, MTProto job, remote-node synchronization,
Telegram/email subscribers, tunnel health monitor, pprof server or subscription
listener is started. Local normal/probe Xray process startup and the lower-level
command launcher also reject execution. These checks are intentionally below
HTTP so a missed caller cannot activate the core merely by invoking Start.

This mode is for inspecting an already drained node before current SaaS desired
state is reconciled. It does not kill old or externally launched processes.
The existing stop/drain and exclusive API ingress prerequisites remain required.
It does not establish full kernel isolation or contain an authorized host root.
It provides no reconciliation write path or promotion to public runtime yet.

Database initialization/migrations remain the pinned panel's startup behavior;
this is read-only HTTP, not a promise of zero SQLite writes. Snapshot/rollback
protection must precede startup, and the old DB is never current business truth.
Node journal version 6 still denies startup after file replacement; its next
integration must verify this exact backend mode before granting a quarantined
start. Normal-mode startup and release require current SaaS authorization.

The packaged CI scenario first proves a healthy real VPN control and disabled
candidate, drains that process group, then starts maintenance with the same
enabled client still in the DB. Authenticated inventory remains available while
write/restart attempts, Xray children and both real VLESS connections are denied.
SIGHUP must retain the mode and client identities. Unit tests cover mode freezing,
invalid values, route/method/auth distinctions, scheduler/subscription exclusion
and normal/probe process launch denial. CI is not deployment or fleet acceptance.
