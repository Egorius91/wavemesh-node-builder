# Node Agent mTLS network lifecycle client

Status: partial implementation; not imported by the running Agent
Date: 2026-07-27
Parent: #29
Local state: #27 / PR #28
SaaS contracts: `Egorius91/wavevpn-saas#23`, `Egorius91/wavevpn-saas#24`
Validation parent: `Egorius91/wavevpn-saas#16`

## Purpose

Add a testable HTTPS/mTLS transport and certificate bootstrap/rotation client around the existing local key/CSR/certificate state, without changing the accepted bearer-only `node_agent.py` runtime or installer.

The module:

```text
agent/node_mtls_client.py
```

is not imported by `agent/node_agent.py` in this increment.

## Authentication modes

```text
bearer
bootstrap-mtls
mtls
```

### bearer

- current compatibility mode;
- requires an existing `wvn_` bearer;
- uses the system server-CA trust store;
- sends the Authorization header;
- does not request a Node certificate.

### bootstrap-mtls

- requires a bearer while no active local mTLS generation exists;
- may request the first certificate with bearer authentication;
- normal observe-only API requests also remain bearer-authenticated until local activation succeeds;
- switches all later requests to mTLS as soon as a validated active generation exists;
- does not delete or rewrite the bearer itself.

### mtls

- does not require or read a bearer token;
- requires an active local identity;
- sends no Authorization header;
- verifies the API hostname and server chain;
- presents the active Node client certificate/private key;
- network/TLS/authentication failure never falls back to bearer.

## Configuration model

The future Agent integration will map environment values into:

```text
api_base
node_id
tenant_id
environment
auth_mode
optional bearer_token
request_timeout_seconds
state_root
```

Proposed default remains:

```dotenv
WAVEMESH_AGENT_AUTH_MODE=bearer
WAVEMESH_AGENT_MTLS_STATE_ROOT=/etc/wavemesh-agent/tls
WAVEMESH_AGENT_MTLS_ENVIRONMENT=staging
```

No environment parsing or installer change is included in this partial PR.

## TLS context

For mTLS requests the client creates an `ssl.SSLContext` with:

```text
SERVER_AUTH purpose
CERT_REQUIRED
hostname verification enabled
minimum TLS 1.2
active CA bundle
active certificate
active private key
```

The private key path is passed directly to Python/OpenSSL. Key content is never read into general runtime metadata or included in request JSON.

## Certificate endpoint

```text
POST /api/internal/v1/nodes/{nodeId}/certificates
Idempotency-Key: node-certificate-<opaque digest>
```

Payload:

```text
csr
agent_version
```

The opaque idempotency key is SHA-256 over Node ID plus CSR request hash. It does not contain the request hash verbatim.

The client requires the response fields:

```text
credential_id
certificate
chain
identity_uri
expires_at
previous_valid_until
already_processed
```

Before local activation it verifies:

- returned identity URI exactly equals the configured environment/tenant/Node URI;
- credential ID is a bounded opaque ID;
- lifecycle timestamps contain timezones;
- replay metadata is boolean;
- response size is bounded.

The existing `NodeMtlsState.activate_pending_certificate()` then verifies:

- CA chain;
- certificate validity;
- exact single URI SAN;
- certificate/private-key match;
- forbidden SAN absence;
- atomic generation activation.

## Response-loss recovery

The local state reuses the same pending private key and CSR after restart.

A repeated call therefore sends:

```text
same CSR
same opaque idempotency key
```

The SaaS lifecycle service replays the same certificate result. The client validates and activates it without generating another key.

Pending state is not cleared by the network client. It is cleared only after `NodeMtlsState` validates and activates the returned certificate.

## Certificate scheduling helpers

The module provides:

```text
parse_certificate_expiry(active_identity)
certificate_rotation_due(expires_at, rotate_before_seconds)
```

The helpers:

- read `notAfter` with OpenSSL without exposing the certificate body;
- require timezone-aware timestamps;
- enforce a bounded rotation threshold;
- use UTC server-style comparison semantics.

Persistent retry/backoff integration with the main Agent loop remains follow-up work.

## HTTP safety

- HTTPS API base is mandatory;
- response body is limited to 256 KiB;
- invalid/non-object JSON is rejected;
- error messages are compacted and bounded;
- HTTP problem details expose only code/retryable/message;
- TLS errors become one retryable `NETWORK_OR_TLS_ERROR`;
- no shell command is used;
- no command polling or execution is introduced.

## Sanitized lifecycle metadata

The helper permits only:

```text
opaque credential ID
expiry timestamp
previous overlap deadline
a replay boolean
opaque local generation ID
```

It excludes:

```text
bearer
request/public-key/fingerprint/serial hashes
private key
CSR
certificate/chain PEM
identity URI
subscription material
```

## Tests

Unit tests cover:

- unchanged bearer default;
- pure mTLS configuration without bearer;
- bearer Authorization behavior;
- bootstrap mode switching to mTLS after activation;
- missing active mTLS identity failing before a network request;
- mTLS network failure without bearer fallback;
- pending CSR reuse and opaque idempotency key;
- exact returned identity validation;
- no activation after wrong identity;
- rotation due boundary;
- sanitized lifecycle metadata;
- insecure API/configuration rejection.

## Remaining work in #29

This partial PR does not close #29. Still required:

1. import the module from `node_agent.py`;
2. parse the new auth-mode/environment/state-root settings;
3. route heartbeat and observations through the selected transport;
4. run initial bootstrap before mTLS-only requests;
5. parse active certificate expiry and invoke rotation only when due;
6. persist retry/backoff metadata in the Agent runtime file;
7. preserve the existing bearer rotation only in bearer mode;
8. add foreground/full-loop integration tests;
9. review installer and systemd read/write paths separately;
10. deploy only through the two-Entry staging validation plan.

## Safety invariants

This increment does not:

- modify `node_agent.py`;
- modify `install.sh` or systemd;
- deploy/restart an Entry Node;
- enable any SaaS gate;
- open enrollment;
- enable command polling/execution;
- modify Xray, 3X-UI, nginx, routes, Exit Nodes, clients or subscriptions.
