# WaveMesh Observe-Only Node Agent

The first Node Agent mode is deliberately read-only. It connects an existing
WaveMesh node to WaveVPN SaaS without changing 3X-UI, nginx, routes, clients, or
subscriptions.

## Guarantees

- no command polling;
- no command execution;
- no direct 3X-UI mutations;
- heartbeat every 60 seconds by default;
- sanitized route-health observation every 5 minutes by default;
- replacement bearer tokens are generated on the node;
- SaaS receives only the SHA-256 hash of a replacement token;
- rotation uses a short overlap window so an interrupted write can be retried;
- a pending replacement token is stored locally as mode `0600` and reused after
  process restart until rotation succeeds;
- fleet scheduling can use deterministic per-Node jitter;
- retryable rotation failures use persistent bounded backoff;
- current single-Node timing remains unchanged unless jitter is explicitly set.

The agent reports route IDs, Exit IDs, enabled state, health state, latency, and
aggregate Exit counts. It omits display names, outbound tags, selectors,
credentials, UUIDs, subscription material, panel paths, domains, and URLs.

## Prerequisites

- an installed WaveMesh node with `/etc/wavemesh-node/config.json`;
- the `wavemesh` CLI in `PATH`;
- Python 3;
- an enrolled agent environment at `/etc/wavemesh-agent/agent.env`;
- `NODE_AGENT_BEARER_ENABLED=true` in SaaS;
- the SaaS credential-rotation endpoint deployed;
- `NODE_COMMAND_WRITES_ENABLED=false` during observe-only acceptance.

A minimal enrolled environment looks like this:

```dotenv
WAVEMESH_API_BASE=https://example.com/api
WAVEMESH_NODE_ID=internal-saas-node-id
WAVEMESH_TENANT_ID=internal-saas-tenant-id
WAVEMESH_AGENT_TOKEN=wvn_REDACTED
WAVEMESH_AGENT_TOKEN_EXPIRES_AT=2026-07-26T10:53:30.357Z
WAVEMESH_AGENT_MODE=observe-only
WAVEMESH_OBSERVED_VERSION=0
WAVEMESH_AGENT_HEARTBEAT_SECONDS=60
WAVEMESH_AGENT_OBSERVATION_SECONDS=300
WAVEMESH_AGENT_ROTATE_BEFORE_SECONDS=21600
WAVEMESH_AGENT_ROTATION_JITTER_SECONDS=0
WAVEMESH_AGENT_ROTATION_RETRY_BASE_SECONDS=30
WAVEMESH_AGENT_ROTATION_RETRY_MAX_SECONDS=900
```

Keep the file owned by `root:root` with mode `0600`.

`WAVEMESH_AGENT_ROTATION_JITTER_SECONDS=0` preserves the existing schedule.
Do not increase it on the active Entry Node without a separate staging rollout
and acceptance. The planned large-fleet validation envelope is `0..900`
seconds, which remains inside the server's wider due window.

## Validate before installation

```bash
sudo python3 agent/node_agent.py check \
  --env-file /etc/wavemesh-agent/agent.env
```

The validation output contains only non-secret configuration metadata. It does
not print the bearer or its hash.

A single foreground cycle can be tested with:

```bash
sudo python3 agent/node_agent.py once \
  --env-file /etc/wavemesh-agent/agent.env
```

This performs credential rotation only when due, refreshes cascade health,
sends one changed-state observation, and sends one heartbeat.

## Install the service

From a checked-out repository branch containing the agent:

```bash
sudo bash agent/install.sh
```

The installer:

1. validates the enrolled environment without printing the bearer token;
2. creates a private backup when the installed version or config will change;
3. installs the Agent and mTLS modules atomically;
4. migrates a missing mTLS mode to explicit `disabled`;
5. installs the hardened `wavemesh-node-agent.service`;
6. reloads systemd only when unit content changed;
7. enables the unit without starting or restarting it.

Deployment and a controlled service restart remain separate reviewed
operations. See `docs/NODE_AGENT_INSTALLER.md` for rollback behavior.

## Operations

```bash
sudo systemctl status wavemesh-node-agent.service --no-pager
sudo journalctl -u wavemesh-node-agent.service --since "10 minutes ago" --no-pager
sudo systemctl restart wavemesh-node-agent.service
```

The logs never print bearer tokens or replacement hashes.

Local state:

```text
/etc/wavemesh-agent/agent.env          permanent active credential, mode 0600
/etc/wavemesh-agent/rotation.pending   temporary crash-safe replacement, mode 0600
/etc/wavemesh-agent/runtime.json       sanitized schedule/retry/observation metadata, mode 0600
```

`rotation.pending` is removed automatically after the new credential has been
saved atomically to `agent.env`.

The runtime file may contain only non-secret operational metadata such as:

```text
rotation_credential_expires_at
rotation_due_at
rotation_jitter_seconds
rotation_retry_attempts
rotation_retry_at
rotation_retry_code
rotation_retryable
last_observation_hash
last_observation_at
```

It must never contain the active token, pending token, replacement hash,
authorization header, subscription material, UUID, domain, URL, selector or
outbound tag.

## Rotation contract

The agent's base rotation target is six hours before local credential expiry.
With jitter enabled, its effective schedule is:

```text
rotation_due_at = expires_at - rotate_before + deterministic_jitter
```

Jitter is derived from the opaque Node ID and credential expiry. It is stable
across process restarts for the same Node/credential, bounded by configuration,
and different across fleet members. No bearer, token hash or other secret is
used in the calculation.

A persisted `rotation.pending` always takes precedence over the initial
schedule. This ensures crash recovery resumes immediately rather than waiting
for a new jitter boundary.

When due, the agent generates a new `wvn_` token, persists it locally, and sends
only its SHA-256 hash to:

```text
POST /api/internal/v1/nodes/{nodeId}/credentials/rotate
```

SaaS creates a replacement credential and shortens the old credential to the
configured overlap window. Retrying with the same replacement hash returns the
same credential result. This makes rotation safe when the HTTP response is
received but the agent process stops before updating `agent.env`.

Recommended SaaS defaults:

```dotenv
NODE_AGENT_TOKEN_TTL_HOURS=24
NODE_AGENT_ROTATE_BEFORE_HOURS=8
NODE_AGENT_ROTATION_OVERLAP_SECONDS=300
```

The local six-hour base threshold plus a maximum planned 15-minute fleet jitter
remains inside the server's eight-hour rotation window.

## Retry and backoff contract

Retryable failures do not cause a request on every heartbeat. The agent persists
an exponentially increasing, bounded retry schedule:

```text
attempt 1: bounded around 30 seconds
attempt 2: bounded around 60 seconds
attempt 3: bounded around 120 seconds
...
maximum: 900 seconds by default
```

The delay uses deterministic equal jitter between one-half and the current
ceiling. It is stable for the same Node, credential expiry and attempt number.

The retry state survives process restart. It is scoped to the current credential
expiry and is automatically cleared when:

- rotation succeeds;
- a replacement credential changes the expiry;
- runtime retry metadata is invalid or belongs to another credential.

For a non-retryable server response, the agent uses the configured maximum delay
instead of creating a tight failure loop. Existing conflict rules still clear an
unusable pending replacement where required.

Logs expose only attempt count, delay and retryable status. They do not expose
the token, replacement hash or pending content.

## Fleet rollout guidance

Keep jitter at zero for a single accepted Node unless a fleet rollout explicitly
changes it.

Before enabling non-zero jitter:

1. confirm the server due window is wider than the local threshold plus maximum jitter;
2. validate `2`, `10`, `100` and `1000` simulated Nodes;
3. confirm retries remain idempotent with the same pending replacement;
4. test simultaneous Agent restart and SaaS outage;
5. verify heartbeat continues while rotation is backed off;
6. verify runtime and logs contain no token/hash;
7. deploy through a separate PR and controlled restart;
8. compare new `MainPID`, service capabilities, Node health and feature gates;
9. retain a rollback to jitter `0` and the prior Agent package.

## Observe-only acceptance

Before enabling command polling or commercial writes, confirm in SaaS that:

- the node stays `ACTIVE`;
- heartbeats remain fresh;
- `command_polling=false`;
- `command_execution=false`;
- health observations contain no connection material;
- credential rotation succeeds before expiry;
- both the old and replacement credentials behave as expected during the short
  overlap, and the old credential is rejected after expiry;
- retry metadata is removed after successful rotation;
- non-zero jitter, when explicitly tested, remains inside the due window;
- no token or hash appears in logs or runtime metadata.
