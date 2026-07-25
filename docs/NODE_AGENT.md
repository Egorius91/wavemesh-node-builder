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
  process restart until rotation succeeds.

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
```

Keep the file owned by `root:root` with mode `0600`.

## Validate before installation

```bash
sudo python3 agent/node_agent.py check \
  --env-file /etc/wavemesh-agent/agent.env
```

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
2. copies the agent to `/usr/local/lib/wavemesh-agent/node_agent.py`;
3. installs a hardened `wavemesh-node-agent.service`;
4. enables and starts the service;
5. fails closed if the service does not become active.

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
/etc/wavemesh-agent/runtime.json       last accepted observation hash, mode 0600
```

`rotation.pending` is removed automatically after the new credential has been
saved atomically to `agent.env`.

## Rotation contract

The agent starts rotation when the local credential has six hours or less
remaining by default. It generates a new `wvn_` token, persists it locally, and
sends only its SHA-256 hash to:

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

The local six-hour threshold is intentionally inside the server's eight-hour
rotation window.

## Observe-only acceptance

Before enabling command polling or commercial writes, confirm in SaaS that:

- the node stays `ACTIVE`;
- heartbeats remain fresh;
- `command_polling=false`;
- `command_execution=false`;
- health observations contain no connection material;
- credential rotation succeeds before expiry;
- both the old and replacement credentials behave as expected during the short
  overlap, and the old credential is rejected after expiry.
