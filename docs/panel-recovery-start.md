# Recovery start under retained maintenance

`PanelStart.started` is an internal installer context for one recovery activation
of the fixed panel service. It does not replace files, restore a database, release
maintenance or grant commercial access. Existing CLI/Agent paths do not call it.
It consumes the prior stopped binding with the same operation, generation,
candidate, backup, helper and boot. Candidate and backup hashes are identity
bindings: independent validation and transactional replacement remain required.

The controller retains both Node and journal locks throughout. Ordinary startup
still checks the canonical journal. When the Node lock is busy, the installed
startup helper may ask a root-only Unix socket under that journal for admission.
There is no environment flag, reusable file permit, shell command or journal
reset. A missing, unsafe or stale endpoint does not grant access.

Before StartUnit, the controller verifies the stopped cgroup, API rules, mandatory
startup guard and service contract. Recovery supports only Type=simple,
Restart=no, empty PIDFile, the fixed `/usr/local/x-ui/x-ui` argv and its working
directory. It verifies protected executable bytes against the supplied digest.
Other unit configurations require deliberate installation work, not a fallback.

Schema v5 retains the complete v4 stop/installation/maintenance record and adds:

- START_INTENT persisted before the one StartUnit call;
- the returned systemd job path persisted before accepting the helper;
- START_ADMITTED with invocation and cgroup identity persisted before OK.

The broker obtains SO_PEERCRED from the socket, pins the peer with a pidfd, and
requires the actual systemd ControlPID in activating/start-pre for the returned
job. Kernel cgroup and network namespace must match. It rechecks the service,
helper/executable contract, API filter and peer before consuming admission. An
ordinary root client with correct protocol bytes is insufficient. Root and the
OS remain trusted; this is not containment against a malicious administrator.

After admission, readback requires the bound invocation active/running, correct
cgroup and no pending job. It compares the actual MainPID executable bytes via
/proc with the expected digest and verifies PID liveness and unchanged identity.
The context yields with both locks retained; ordinary writes remain closed.

An unknown StartUnit result, failed persistence, lost grant, failed activation,
new boot/invocation, or mismatched contract never causes another start or grant.
Re-entry only accepts an already-admitted, still-running matching invocation.
START_INTENT without proven admission remains reconciliation-required. A broker
crash leaves v5 and potentially a stale socket; it cannot be used to retry. No
automatic stop, restart, reset-failed, cancellation or release occurs. An admission
which reached the helper before controller death may finish activation; the
durable hold and API isolation remain in place and later readback must reconcile.

CI combines the installed helper, actual systemd job/ControlPID, nft rules and a
synthetic child service. It injects a lost successful start result, verifies
read-only reconciliation and healthy child traffic, and keeps ordinary writers
closed. Root unit tests cover unknown dispatch, failure before grant, lost grant,
wrong peer, stale socket, strict schema and retained v5 denial.
This is not actual panel/Xray installation, rollback, reboot or staging acceptance.
The complete actuator still must validate/replace/restore artifacts and database,
activate compatible interlocks transactionally, reconcile SaaS ownership and
prove actual panel/Xray traffic before reopening admission.

Protocol references: [systemd D-Bus manager and jobs](https://wiki.freedesktop.org/www/Software/systemd/dbus/),
[Linux Unix socket credentials](https://man7.org/linux/man-pages/man7/unix.7.html).
