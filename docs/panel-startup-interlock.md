# Panel startup admission

The installed shared guard supports `--check-startup`. The packaged
`systemd/50-wavemesh-panel-startup.conf` template adds a mandatory `ExecStartPre`
to the future managed `x-ui.service`. CLI installation copies the template under
`/usr/local/lib/wavemesh/systemd`; it does not activate a drop-in, reload systemd,
stop/start a service or initialize the journal.

The command uses the canonical `/var/lib/wavemesh-agent/panel-requests` directory
regardless of environment overrides. It requires Linux/root, root-owned protected
directory ancestors, safe journal files, and both Node/journal locks. Missing or
corrupt state, unsupported schemas, pending requests, held maintenance and v3
installation intent deny startup. Only a valid accepted v1 request or a cancelled
v2 hold without a pending request allows it. Checks never alter `state.json` or
release a hold. Losing volatile locks does not discard the durable denial.
Lock acquisition may create a missing lock file; no missing state is bootstrapped.

The drop-in uses isolated Python and a fixed installed path. Failed pre-start
commands prevent `ExecStart`; see the official
[systemd service specification](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml).
It does not use an optional condition, an ignored-error prefix, or a success
fallback. Root service identity is required. A missing helper also denies start.

This is admission for a **new activation**, not continuous exclusion or proof
that the panel/Xray is stopped. A start admitted before a hold may still execute.
Other pre-start/stop-post hooks, alternate units and direct root launches are not
controlled by this template. Before installation the actuator must verify the
effective unit and all activation paths, retain ingress isolation, stop the
backend, verify descendant drain, and revalidate under the operation locks.

Deployment must durably activate and read back the gate before entering effects.
An empty/uninitialized journal needs an explicit validated migration/bootstrap;
do not fabricate an accepted request or cancel installation to permit startup.
A v3 recovery start requires a separate operation-bound protocol that retains
ingress exclusion through candidate validation. This PR supplies no bypass,
release, recovery-start authorization or safe upgrade procedure. Existing
transaction rollback starts are not compatible with a held Node lock once this
gate is activated; do not deploy it independently of the complete actuator.
Root remains trusted; restoring an old valid journal/whole disk needs external
authority reconciliation and is not detected by this local check.

Dedicated Linux CI runs strict state/lock tests and the actual installed helper
through a temporary systemd service. Only fixture paths in its private installed
copy are relocated; production CLI has no path override. CI proves healthy
accepted/cancelled starts, blocked pending/held/installation starts, ignored
environment redirection, unchanged state, repeated startup after volatile lock
loss and missing/corrupt state denial. It does not reboot a machine, run a real
panel in this fixture or prove the complete installer is production-ready.
