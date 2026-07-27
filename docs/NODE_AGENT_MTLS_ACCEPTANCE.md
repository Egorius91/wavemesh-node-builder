# One Entry Node mTLS shadow acceptance

Status: prepared, not executed
Date: 2026-07-27
Parent: #29

## Safety boundary

This runbook prepares a bounded acceptance for one existing staging Entry
Node. Merged code is not deployed code. This document does not authorize a
deployment, a server login, a feature-gate change, certificate issuance, an
Agent restart, or an Entry Node mutation.

Every such action has an explicit `OPERATOR APPROVAL REQUIRED` checkpoint
below. Stop if the approved commit, backup, rollback package, staging target,
or sanitized evidence directory is not known.

Never print or copy bearer values or hashes, Node/Tenant IDs, complete identity
URIs, subscription URLs, private keys, CSR/certificate PEM, certificate
serials/fingerprints, public-key hashes, delivery keys, Vault tokens, or raw
Agent logs. Evidence consists only of merged commit IDs, CI conclusions,
booleans, aggregate counts, fixed status/blocker codes, and rounded operational
timestamps.

## Invariants

Keep these gates false throughout the acceptance:

```text
NODE_ENROLLMENT_ENABLED=false
NODE_AGENT_MTLS_ENABLED=false
NODE_COMMAND_WRITES_ENABLED=false
INTERNAL_COMMERCIAL_WRITES_ENABLED=false
ACCESS_SHADOW_ENABLED=false
NODE_EXIT_RECONCILIATION_ENABLED=false
NODE_ASSIGNMENT_WRITES_ENABLED=false
```

Keep `NODE_AGENT_BEARER_ENABLED=true`. The independently scoped
`NODE_MTLS_CERTIFICATE_LIFECYCLE_ENABLED` may change only at its explicit
checkpoint. Shadow mode is an additional Agent request path; bearer heartbeat
and observations remain authoritative.

Use the accepted WaveVPN SaaS documents as the source of truth:

- `docs/operations/MTLS-STAGING-MANUAL-CHECKLIST.md`
- `docs/operations/MTLS-TRUSTED-INGRESS-STAGING.md`
- `docs/operations/MTLS-VAULT-PKI-STAGING.md`
- `docs/operations/MTLS-CERTIFICATE-LIFECYCLE-STAGING.md`
- `docs/operations/TWO-ENTRY-STAGING-INCIDENT-RUNBOOK.md`

## Sanitized local collector

`acceptance.py` is read-only. It invokes fixed `systemctl show`,
`node_agent.py check`, bounded `journalctl`, and `openssl x509 -enddate`
commands without a shell. It never forwards raw subprocess output.

Phases:

- `baseline`: current pre-deployment bearer service;
- `disabled`: accepted package is active with mTLS disabled;
- `shadow`: settled `SHADOW_ACTIVE` state and valid private active generation;
- `rollback`: bearer service after rollback; pre-mTLS Agent check output is
  accepted.

Exit `0` means `PASS`; exit `1` means `BLOCKED`. A blocker is a stop condition,
not permission to repair staging ad hoc.

## Phase 0 — identify the accepted artifacts

Record the exact merged Builder and SaaS commits and the green required-check
conclusions outside the shell session. Do not use a moving branch name as a
deployment artifact.

```bash
# [SAAS] read-only
set -Eeuo pipefail
git rev-parse --verify HEAD
git status --short
```

```bash
# [ENTRY NODE] read-only, in the accepted Builder checkout
set -Eeuo pipefail
git rev-parse --verify HEAD
git status --short
```

Stop if either checkout is dirty, a commit differs from the approved record,
or required CI is not green.

## Phase 1 — SaaS backup and preflight

`OPERATOR APPROVAL REQUIRED`: permission to access staging and run the existing
protected database backup procedure. Do not invent a new backup command here.
Record only backup UTC time, opaque backup reference, protected location
confirmed, and isolated restore procedure confirmed.

After the backup succeeds, create a private evidence directory and run the
compiled read-only preflight from the accepted SaaS application runtime:

```bash
# [SAAS] read-only application/DB query; writes only a private evidence file
set -Eeuo pipefail
umask 077
EVIDENCE_DIR="${EVIDENCE_DIR:?set a private evidence directory}"
install -d -m 0700 "$EVIDENCE_DIR"
node apps/api/dist/scripts/mtls-staging-preflight.js \
  > "$EVIDENCE_DIR/saas-preflight-before.json"
```

The report must be `SAFE_BASELINE`, contain no blocking codes, show every
protected gate above as false, bearer as true, and all credential-invariant
anomaly counters as zero. Before lifecycle enablement, readiness must show:

```text
trusted_ingress_secret_configured=true
production_issuer_configured=true
certificate_delivery_configured=true
node_mtls_enabled=false
node_bearer_enabled=true
```

Record aggregate fleet evidence: the selected Entry remains `ACTIVE` and
`IN_SYNC`, and the expected Entry/Exit health is unchanged. Do not query or
export credential hash columns.

## Phase 2 — Entry Node bearer baseline

`OPERATOR APPROVAL REQUIRED`: permission to access the single selected staging
Entry Node for read-only checks.

From the accepted Builder checkout:

```bash
# [ENTRY NODE] read-only
set -Eeuo pipefail
umask 077
EVIDENCE_DIR="${EVIDENCE_DIR:?set a private evidence directory}"
install -d -m 0700 "$EVIDENCE_DIR"
sudo python3 agent/acceptance.py baseline \
  > "$EVIDENCE_DIR/entry-baseline.json"
```

Require `PASS`, active/enabled service, zero restarts, safe unit/environment
checks, and zero log secret-pattern counts. Confirm the existing bearer
heartbeat remains fresh through the SaaS aggregate/read-safe view.

## Phase 3 — install with mTLS disabled

`OPERATOR APPROVAL REQUIRED`: permission to run the accepted Builder installer
on the selected Entry Node. The installer prepares files and backups; it does
not start or restart the service.

```bash
# [ENTRY NODE] mutation: package files, private backup, systemd enable/reload
set -Eeuo pipefail
sudo bash agent/install.sh
```

Verify the installer reported that activation remains separate.

`OPERATOR APPROVAL REQUIRED`: permission to restart only the Node Agent so the
accepted package becomes active.

```bash
# [ENTRY NODE] mutation: approved single-service restart
set -Eeuo pipefail
sudo systemctl restart wavemesh-node-agent.service
```

After at least two normal Agent cycles:

```bash
# [ENTRY NODE] read-only
set -Eeuo pipefail
umask 077
EVIDENCE_DIR="${EVIDENCE_DIR:?set a private evidence directory}"
sudo /usr/local/lib/wavemesh-agent/acceptance.py disabled \
  > "$EVIDENCE_DIR/entry-disabled.json"
```

Require `PASS`, `BEARER_ONLY`, zero restarts after the controlled restart
baseline is established, fresh bearer heartbeat, and no Node/Xray/3X-UI/nginx,
route, client, or subscription change.

## Phase 4 — trusted ingress, issuer, and delivery readiness

If the Phase 1 readiness fields are already true, perform no infrastructure
change. Otherwise stop and use the existing SaaS trusted-ingress, Vault PKI,
and certificate-lifecycle runbooks. Each overlay, secret-file mount, DNS/TLS
change, Nginx reload, API image change, and issuer action requires separate
operator approval and its own rollback evidence.

After any separately approved readiness work, rerun:

```bash
# [SAAS] read-only
set -Eeuo pipefail
umask 077
EVIDENCE_DIR="${EVIDENCE_DIR:?set a private evidence directory}"
node apps/api/dist/scripts/mtls-staging-preflight.js \
  > "$EVIDENCE_DIR/saas-preflight-ready.json"
```

Do not continue until all three readiness fields are true, bearer is true,
the global mTLS authentication gate is false, and the protected gates remain
false.

## Phase 5 — enable only the lifecycle gate

`OPERATOR APPROVAL REQUIRED`: permission to set only
`NODE_MTLS_CERTIFICATE_LIFECYCLE_ENABLED=true` in staging and recreate only the
approved API component with the accepted lifecycle overlay. Follow the SaaS
certificate-lifecycle runbook; do not copy secret values into a command,
environment file, terminal transcript, or evidence.

Immediately rerun the compiled SaaS preflight. The only expected gate delta is
the lifecycle gate. `NODE_AGENT_MTLS_ENABLED` remains false and bearer remains
true. Stop and roll the lifecycle gate back to false if any other gate changes
or a blocker appears.

## Phase 6 — one Node in local shadow mode

`OPERATOR APPROVAL REQUIRED`: permission to edit the selected Node's private
Agent environment using the approved secret/configuration procedure. Set only
the documented `WAVEMESH_AGENT_MTLS_*` paths and
`WAVEMESH_AGENT_MTLS_MODE=shadow`; never paste file contents into the
environment. Preserve bearer configuration.

`OPERATOR APPROVAL REQUIRED`: permission to restart only the Node Agent.

```bash
# [ENTRY NODE] mutation: approved single-service restart
set -Eeuo pipefail
sudo systemctl restart wavemesh-node-agent.service
```

Allow normal Agent cycles to bootstrap, validate, atomically activate, and
acknowledge the first certificate. Do not invoke issuance endpoints manually.

```bash
# [ENTRY NODE] read-only
set -Eeuo pipefail
umask 077
EVIDENCE_DIR="${EVIDENCE_DIR:?set a private evidence directory}"
sudo /usr/local/lib/wavemesh-agent/acceptance.py shadow \
  > "$EVIDENCE_DIR/entry-shadow.json"
```

Require `PASS`, `SHADOW_ACTIVE`, a valid contained active generation, no
pending acknowledgement, zero restart count, and zero log secret-pattern
counts. The certificate expiry timestamp is allowed evidence; serial,
fingerprint, subject/SAN, and PEM are not.

Rerun the SaaS preflight and retain only its sanitized JSON. Require one
additional active mTLS credential in aggregate, zero invalid/unrecoverable
credentials, selected Entry `ACTIVE` and `IN_SYNC`, unchanged expected health,
and continuing bearer heartbeat/observations.

## Phase 7 — observation window

Observe multiple normal Agent cycles without changing gates or configuration.
At the start and end:

```bash
# [ENTRY NODE] read-only
set -Eeuo pipefail
umask 077
EVIDENCE_DIR="${EVIDENCE_DIR:?set a private evidence directory}"
sudo /usr/local/lib/wavemesh-agent/acceptance.py shadow \
  > "$EVIDENCE_DIR/entry-shadow-observation.json"
```

```bash
# [SAAS] read-only
set -Eeuo pipefail
umask 077
EVIDENCE_DIR="${EVIDENCE_DIR:?set a private evidence directory}"
node apps/api/dist/scripts/mtls-staging-preflight.js \
  > "$EVIDENCE_DIR/saas-preflight-observation.json"
```

Require bearer and shadow heartbeats to remain healthy, the Node to remain
`ACTIVE`/`IN_SYNC`, expected health to remain unchanged, zero restarts, zero
secret-pattern counts, and no new blocker.

## Phase 8 — controlled rotation

Prefer a naturally due rotation. An accelerated staging lifetime or rotation
threshold is a configuration mutation and requires separate operator approval
plus confirmation that it remains within the bounds documented by the SaaS
and Agent implementations.

Do not call an issuance endpoint by hand. Observe the Agent's normal
`ROTATING` to `SHADOW_ACTIVE` transition, then repeat both Phase 7 collectors.
Acceptance requires a new active aggregate lifecycle result, an acknowledged
delivery, the old overlap handled by policy, uninterrupted bearer operation,
zero restarts, and no secret-bearing evidence.

## Phase 9 — rollback drill

Rollback is mandatory before declaring the procedure executable.

`OPERATOR APPROVAL REQUIRED`: permission to set the local Agent mTLS mode back
to `disabled` with the approved private configuration procedure. Preserve the
mTLS state directory and bearer configuration.

`OPERATOR APPROVAL REQUIRED`: permission to restore the accepted private
installer backup. The restore does not restart by default.

```bash
# [ENTRY NODE] mutation: atomic package/config restore, no restart
set -Eeuo pipefail
sudo /usr/local/sbin/wavemesh-node-agent-rollback --latest
```

`OPERATOR APPROVAL REQUIRED`: permission to restart only the Node Agent.

```bash
# [ENTRY NODE] mutation: approved single-service restart
set -Eeuo pipefail
sudo systemctl restart wavemesh-node-agent.service
```

```bash
# [ENTRY NODE] read-only
set -Eeuo pipefail
umask 077
EVIDENCE_DIR="${EVIDENCE_DIR:?set a private evidence directory}"
sudo python3 agent/acceptance.py rollback \
  > "$EVIDENCE_DIR/entry-rollback.json"
```

Run this from the still-present accepted Builder checkout because the restored
older package may not include the collector. Require `PASS`, bearer heartbeat
restored without bearer reissue, no restart loop, unchanged Node
traffic/configuration, and no secret-pattern findings.

`OPERATOR APPROVAL REQUIRED`: permission to set only the SaaS lifecycle gate
back to false and recreate only the approved API component. Keep bearer true
and global mTLS auth false. Rerun the SaaS preflight and require a safe bearer
baseline. Do not delete credential rows or additive schema.

## Stop conditions

Stop immediately on any collector `BLOCKED` result, failed backup/restore,
unexpected gate delta, bearer interruption, Node not `ACTIVE`/`IN_SYNC`,
health regression, restart loop, unsafe file/symlink/mode, expired certificate,
unrecoverable or unacknowledged delivery, cross-Node/environment result,
secret-pattern count above zero, or any Xray/3X-UI/nginx/route/client/
subscription change.

Preserve only sanitized evidence, disable only the lifecycle gate if it was
enabled, return the local Agent to disabled/bearer-only mode, and follow the
incident runbook. Do not improvise direct SQL cleanup or secret rotation.

## Completion record

The acceptance record is complete only when it contains:

- exact approved commits and green required checks;
- protected backup and isolated-restore confirmation;
- sanitized SaaS preflight at each checkpoint;
- `baseline`, `disabled`, `shadow`, observation, rotation, and `rollback`
  collector results;
- continuing bearer evidence, selected Entry `ACTIVE`/`IN_SYNC`, and unchanged
  expected health;
- zero restart and secret-pattern findings;
- successful rollback without bearer reissue;
- operator approvals and UTC timestamps for every mutation.

This record proves one-Node staging acceptance only. It does not authorize
fleet rollout, global mTLS authentication, bearer disablement, production
deployment, or command/commercial/access/reconciliation/assignment writes.
