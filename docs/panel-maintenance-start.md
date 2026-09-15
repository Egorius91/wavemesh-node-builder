# Replacement maintenance admission

`PanelMaintenanceStart` is an internal installer API. It admits a single
systemd activation of an approved replacement into the immutable maintenance
backend introduced by PR78. It does not release commercial access, reconcile
SaaS ownership, edit the service configuration, or expose an Agent command.

Before dispatch it requires all of the following under the retained Node and
request-journal locks:

- Same operation/generation, installation intent and current-boot stop proof.
- A `REPLACED` v6 journal; no partial replacement or rollback phase is accepted.
- An independently approved manifest digest and builder head, verified private
  candidate and rollback snapshot, and the exact supported upstream patch.
- The receipt-bound candidate at the live location and original tree in its
  private rollback slot, including inode, contents and modes.
- Existing kernel API isolation, verified startup helper and exact simple
  systemd unit/ExecStart binary contract.
- Exactly one `WAVEMESH_PANEL_MODE=maintenance` in the loaded Environment,
  without EnvironmentFiles, PassEnvironment or UnsetEnvironment. Only the
  documented basic process/XUI environment keys are supported. The entire
  validated Environment is hashed into the admission contract.

The caller must arrange and verify the maintenance service configuration while
the backend is stopped. This API never repairs drift. Trusted root and the
systemd manager remain inside the administrative trust boundary.

The transition to v7 persists `START_INTENT` before one StartUnit call. The
existing peer/cgroup/job/invocation broker persists `START_ADMITTED` before its
single success response. Running-state verification checks the loaded binary
and actual process startup environment. The pinned backend captures its mode
before loading application environment files; API/settings/SIGHUP cannot remove
maintenance. Unknown dispatch or acknowledgment never causes another start or
grant. Reentry can verify an already running bound invocation only.

v7 retains both replacement and start evidence. Legacy recovery-start, stop,
replacement/rollback and maintenance cancellation do not downgrade it. A stopped
or failed admitted invocation requires a subsequent explicit recovery protocol;
there is no automatic restart or rollback after startup. This is deliberate:
startup may have initialized/migrated the database, so the old file-only rollback
is no longer sufficient.

The full candidate CI retains the previous four reports and adds
`maintenance-start-smoke.json`. It performs actual replacement, rejects missing
mode before dispatch, starts through the real systemd broker, loses the result
and reconciles without another start/grant. It checks authenticated inventory,
denied HTTP writes and real VLESS, SIGHUP, durable Agent/CLI exclusion after lock
release and denial of a second activation after stop.

This establishes maintenance admission only. Current SaaS subscription/ownership
and revocation reconciliation, controlled restoration of VPN runtime, recovery
after an interrupted maintenance start, promotion policy and staging acceptance
remain separate required work for commercial readiness.
