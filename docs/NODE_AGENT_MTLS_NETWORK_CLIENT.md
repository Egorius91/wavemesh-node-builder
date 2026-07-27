# Node Agent mTLS shadow lifecycle

Status: integrated, disabled by default
Date: 2026-07-27
Parent: #29

## Scope

The observe-only Node Agent can run a second, isolated mTLS lifecycle in
`shadow` mode while its existing bearer observation and heartbeat path remains
authoritative. This change does not modify the installer or systemd unit, does
not deploy a Node, does not enable a SaaS gate, and never disables or deletes
the bearer credential.

The implementation is split across:

- `agent/node_mtls_state.py`: local key, CSR, certificate generations, and
  pending acknowledgement journal;
- `agent/node_mtls_client.py`: strict HTTPS/bearer/mTLS lifecycle transport;
- `agent/node_mtls_runtime.py`: finite lifecycle state machine and retry state;
- `agent/node_agent.py`: bearer-first loop integration and shadow heartbeat.

## Modes and states

`WAVEMESH_AGENT_MTLS_MODE` accepts:

- `disabled` (default): no mTLS state directory is touched;
- `shadow`: lifecycle and an additional mTLS heartbeat are attempted without
  changing bearer traffic.

Persisted states are:

```text
BEARER_ONLY
ENROLLING
SHADOW_READY
SHADOW_ACTIVE
ROTATING
FALLBACK
BLOCKED
```

Failures in the mTLS lifecycle or shadow heartbeat are isolated from bearer
token rotation, observation, and heartbeat. A retryable failure uses bounded
exponential backoff. The attempt limit moves the mTLS state to
`BLOCKED`; there is no infinite request loop. Retry jitter defaults to
zero and, when enabled, is deterministic for Node ID and attempt number.

## Configuration

The current integration reads:

```dotenv
WAVEMESH_AGENT_MTLS_MODE=disabled
WAVEMESH_AGENT_MTLS_API_BASE=https://mtls-entry.example.invalid/api
WAVEMESH_AGENT_MTLS_ENVIRONMENT=staging
WAVEMESH_AGENT_MTLS_STATE_ROOT=/etc/wavemesh-agent/tls
# Optional private ingress CA. Omit to use the operating-system trust store.
WAVEMESH_AGENT_MTLS_SERVER_CA_FILE=/etc/wavemesh-agent/ingress-ca.crt
WAVEMESH_AGENT_MTLS_ROTATE_BEFORE_SECONDS=21600
WAVEMESH_AGENT_MTLS_RETRY_BASE_SECONDS=30
WAVEMESH_AGENT_MTLS_RETRY_MAX_SECONDS=900
WAVEMESH_AGENT_MTLS_RETRY_MAX_ATTEMPTS=8
WAVEMESH_AGENT_MTLS_RETRY_JITTER_SECONDS=0
```

The bearer API base and the mTLS API base are deliberately distinct. Shadow
mode requires an HTTPS mTLS base. The optional server CA path must be an
absolute, non-symlink regular file.

## Trust boundaries

The private key and CSR are generated locally. The SaaS response cannot choose
the expected identity: the Agent derives the exact URI from configured
environment, tenant, and Node IDs. Before activation it verifies:

- the certificate chain;
- current validity;
- private-key/public-key match;
- exactly one expected URI SAN;
- absence of DNS, IP, and email SANs.

Each activated generation is root-only. `client.crt` stores the leaf,
`client-chain.crt` stores the leaf plus issuer chain for TLS presentation, and
`ca.crt` stores the client issuer chain used to validate the delivered
certificate. Server authentication is separate: it uses the OS trust store or
`WAVEMESH_AGENT_MTLS_SERVER_CA_FILE`. The client issuer chain is never
implicitly trusted as the server TLS CA.

Activation uses an atomic `active` symlink replacement and retains old
generations for overlap and rollback. Private files use mode `0600` and
directories use `0700` on POSIX systems.

## Crash-safe delivery and acknowledgement

Certificate issuance and rotation reuse the same pending key and CSR until a
delivery is activated. The idempotency key is an opaque digest derived from
Node ID and the local request hash.

Before activation, the Agent persists a private acknowledgement journal that
contains only bounded correlation metadata. Recovery is deterministic:

1. If the response was lost before the journal was written, the same CSR and
   idempotency key replay the same delivery.
2. If the journal exists but activation did not finish, the Agent retrieves
   that opaque credential delivery and activates it.
3. If activation finished but acknowledgement did not, the matching active
   request is acknowledged and the journal is cleared.

Bootstrap, retrieval, and acknowledgement use the still-valid bearer recovery
path. Rotation issuance uses mTLS once an active certificate exists. This
preserves recovery without automatically downgrading an individual mTLS
request to bearer.

## Logging and persisted runtime

General runtime state contains only state name, bounded retry count, next retry
time, a sanitized error code, and schema version. Heartbeat capability data is
limited to mode, state, retry count, retry time, and safe code.

The integration does not log or place in general runtime:

- bearer tokens or full request URIs;
- private keys, CSRs, certificate or chain PEM;
- identity URIs;
- fingerprints, serial numbers, or public-key hashes;
- subscription or client material.

## Rollback

Set `WAVEMESH_AGENT_MTLS_MODE=disabled` and restart the Agent in the later
installer/systemd phase. Bearer operation remains unchanged and does not
require token recovery. Do not delete mTLS state during rollback; preserving it
keeps the operation reversible and avoids generating another identity.

## Deferred work

Installer copying, environment rendering, systemd read/write paths, staging
deployment, SaaS gate enablement, and live acceptance remain separate changes.
