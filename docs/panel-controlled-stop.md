# Controlled panel stop context

`agent/panel_stop.py` joins installation intent, nft readback and the installed
startup guard before one fixed `systemctl stop x-ui.service`. There is no CLI,
remote command, restart, replacement or release. Existing installer/rollback
paths do not call it yet; do not use it as a complete installation procedure.

The caller supplies exact installation and helper digests. They are identity
bindings, not proof that a candidate/backup was independently validated. The
module requires the canonical journal, the same network namespace as the service,
exact operation-bound API rules, protected root-owned helper bytes matching the
supplied SHA256, and systemd's effective mandatory startup argv via D-Bus. Extra
pre/post/stop/condition hooks, ignored guard errors, alternate root filesystems
and bind overlays reject. This validates a limited service contract, not every
privileged launch path or the panel binary itself.

PrivateNetwork and JoinsNamespaceOf are explicitly unsupported, as are MountImages,
ExtensionImages and ExtensionDirectories. D-Bus types and empty/default values
are checked exactly; missing/unknown properties fail closed. An active service's
actual `/proc/MainPID/ns/net` must match the controller, with pidfd liveness and
fresh unit PID/invocation/cgroup observations bracketing the read. This catches
declaration/runtime mismatch after daemon-reload without signalling numeric PIDs.
The verified namespace identity and execution settings are part of the retained
contract digest. See the official [systemd execution specification](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml).

Before dispatch it persists schema v4 STOP_INTENT, binding the operation's
installation to boot ID, service invocation, original cgroup inode and verified
unit/helper contract. Maintenance cannot cancel v4; startup rejects it; old
readers reject its unknown schema. Public status reveals only STOP_INTENT, not
private process/boot identity. A save failure sends no stop.

Stop timeout/lost response retains v4. Re-entry only observes and verifies; it
never sends a second stop. A still-active service, new invocation, new boot,
changed contract or replaced/populated cgroup requires reconciliation. There is
no automatic clear/reset/retry. A successful readback requires inactive/dead,
zero main/control PID and an empty or removed original cgroup-v2 hierarchy.
The kernel's populated flag includes descendants; see the
[kernel cgroup-v2 specification](https://www.kernel.org/doc/html/v6.14/admin-guide/cgroup-v2.html).
The context yields while the Node/journal locks remain held and the API rules
still match. A receipt outside that context is not authority for later effects.

Root and the OS remain trusted. Processes moved outside the service hierarchy,
separate Xray units, alternate launchers, other privileged administrators and
external business ownership are not covered. Overall quiescence remains
NOT_PROVEN. Inactive after a failed stop is not inferred from exit status alone;
failed/transitional service states reject even when processes appear absent.

Root CI uses a dedicated service in a private network namespace, the installed
startup helper and real nft rules. It opens a live echo connection to a child
which ignores SIGTERM; the fixture explicitly uses KillSignal=SIGKILL and marks
that fixture-only signal as expected with SuccessExitStatus=SIGKILL. It verifies
original parent/child exit via pidfds and connection teardown, injects loss after
the real stop, reconciles without redispatch, and proves startup remains blocked.
This is a controlled process fixture, not the actual panel/Xray or a machine
reboot. Packaged panel/VLESS regression is separate. Actual panel drain under its
effective production signal/unit settings remains a later acceptance gate.
The same fixture rejects five unsupported namespace/image declarations before
stop or journal mutation, and checks actual process namespace mismatch after a
declared namespace change. No image is mounted and no host firewall is modified.

The next installer stage must consume this context for verified backup/replace
and introduce operation-bound recovery-start authorization without removing the
hold or bypassing ingress isolation. Journal validation/publication, startup
interlock activation and restore/rollback compatibility remain part of that
complete actuator; no live deployment is authorized by this component.
