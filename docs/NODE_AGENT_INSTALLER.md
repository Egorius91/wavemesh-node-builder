# Node Agent installer and rollback

Status: prepared, not deployed
Date: 2026-07-27
Parent: #29

## Safety boundary

`agent/install.sh` prepares a shadow-capable Agent installation but does not
start or restart the service. It does not enable a SaaS gate, issue a
certificate, generate a CA, or change bearer credentials.

The installer:

- requires root for real system paths;
- installs `node_agent.py`, `node_mtls_client.py`, `node_mtls_runtime.py`, and
  `node_mtls_state.py`, plus the read-only `acceptance.py` collector;
- uses same-directory temporary files and atomic rename for every installed
  file;
- creates `/etc/wavemesh-agent` and its mTLS state tree with mode `0700`;
- installs executable entry points with mode `0755` and Python modules/unit
  files with mode `0644`;
- preserves the root-run systemd architecture and existing hardening;
- adds `WAVEMESH_AGENT_MTLS_MODE=disabled` only when that setting is absent;
- rejects an environment file containing a PEM envelope;
- makes a private backup only when file content or configuration will change;
- runs `daemon-reload` only when unit content differs;
- enables the unit without `--now`;
- leaves activation to a separate operator-controlled deployment step.

Private keys, certificates, issuer chains, and acknowledgement/runtime state
are created later by the Agent with mode `0600`. The environment contains only
configuration values and paths, never PEM material.

## Backup

Changed installations create a timestamped root-only directory under:

```text
/var/lib/wavemesh-agent/backups
```

Each backup records the previous known Agent modules, service unit, rollback
command, and `agent.env`. An `.absent` marker distinguishes a file that did not
exist from an incomplete backup. The manifest contains only schema version and
creation time.

No backup content is printed. Re-running an already-matching installer does not
create another backup or reload systemd.

## Rollback

The installer places:

```text
/usr/local/sbin/wavemesh-node-agent-rollback
```

An operator selects either `--latest` or an exact backup ID. Restore is atomic,
the environment is checked for PEM material, and `daemon-reload` runs only
when the restored unit differs.

Rollback does not restart by default. `--restart` is an explicit deployment
action and restarts only `wavemesh-node-agent.service`. Restoring a previous
Agent/config does not require bearer token reissuance.

## Test-only staging root

`WAVEMESH_AGENT_DESTDIR` and `WAVEMESH_AGENT_SYSTEMCTL` support an isolated
filesystem and fake systemctl for CI smoke tests. Production use leaves both
unset. A destination root must be absolute, must not equal `/`, and must not be
a symlink.

Smoke tests verify module installation, modes, disabled migration, PEM
rejection, private backups, idempotency, rollback, conditional daemon reload,
no implicit restart, systemd hardening, and systemd unit parsing.

The prepared one-Entry-Node activation, shadow, rotation, and rollback
procedure is `docs/NODE_AGENT_MTLS_ACCEPTANCE.md`. Reading that runbook does
not authorize its server-side steps.
