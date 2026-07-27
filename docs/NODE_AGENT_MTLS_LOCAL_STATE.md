# Node Agent mTLS local identity state

Status: integrated, packaged, and disabled by default
Date: 2026-07-27
Parent: #27
Architecture: `Egorius91/wavevpn-saas#11`
Validation: `Egorius91/wavevpn-saas#16`

## Purpose

Prepare and validate local mTLS private-key, CSR and certificate generations while preserving the bearer-authenticated observe-only Agent path.

The module:

```text
agent/node_mtls_state.py
```

is imported only by the gated shadow runtime and is installed by
`agent/install.sh`. Disabled mode does not touch the state root.

## State layout

Proposed root:

```text
/etc/wavemesh-agent/tls/
  pending/
    client.key
    client.csr
    metadata.json
  generations/
    <certificate-derived-generation>/
      client.key
      client.crt
      client-chain.crt
      ca.crt
      metadata.json
  active -> generations/<generation>
```

Permissions:

```text
tls root: 0700
pending directory: 0700
generations directory: 0700
private keys: 0600
CSR/pending metadata: 0600
active generation metadata: 0600
certificate/CA bundle: 0600
```

All files under the Agent TLS state root are private even when their cryptographic content is public. This avoids permission drift and keeps the entire identity bundle behind one filesystem policy.

Production deployment must also ensure `root:root` ownership. Unit tests run as the CI user and validate permission modes only.

## Pending request

`prepare_pending_request()`:

1. rejects symlinked state roots/directories;
2. fails closed when only part of pending state exists;
3. generates an ECDSA P-256 private key locally with OpenSSL;
4. creates a SHA-256 CSR with a non-identity subject;
5. verifies CSR proof of possession;
6. derives SHA-256 hashes of CSR DER and public-key DER;
7. stores only hashes, profile version, algorithm and timestamp in metadata;
8. reuses the existing validated pending key/CSR after restart.

The CSR intentionally contains no trusted Node URI identity. SaaS/issuer policy must generate the identity SAN.

Private key content never leaves the local state module.

## Certificate activation

`activate_pending_certificate()` accepts certificate and CA PEM returned by a future authenticated API client and requires an exact expected identity URI.

Before activation it verifies:

- CA chain;
- certificate is not expired;
- certificate public key matches the pending private key;
- exactly one URI SAN is present;
- URI SAN exactly equals the expected environment/tenant/Node identity;
- DNS, IP and email SAN types are absent.

A certificate-derived generation directory is written and fsynced before an atomic `active` symlink replacement.

Pending key/CSR/metadata are removed only after the generation is validated and the active symlink has been switched.

## Crash behavior

### Before CSR creation completes

Temporary files are removed. No complete pending state is published.

### After pending state is published

A restart revalidates and reuses the same private key and CSR.

### Certificate validation failure

Pending state remains intact. No active generation is created.

### After generation creation but before symlink switch

The complete generation remains available. A repeated activation with the same certificate validates and reuses the generation.

### After symlink switch but before pending cleanup

The active generation is already valid. A future integration layer must detect this state and complete idempotent cleanup rather than generating a new key.

The current isolated module performs cleanup in the same call but the network lifecycle integration still needs explicit recovery tests.

## Metadata minimization

Pending metadata contains only:

```text
created_at
key_algorithm
profile_version
public_key_hash
request_hash
```

Generation metadata contains only:

```text
activated_at
generation
identity_uri_hash
public_key_hash
request_hash
```

It does not contain:

- private key;
- CSR or certificate PEM;
- full identity URI;
- bearer or bearer hash;
- certificate fingerprint;
- subscription material;
- domains, routes, selectors or Xray UUIDs.

## OpenSSL boundary

The module invokes an explicitly selected OpenSSL binary with argument arrays and no shell.

Operations are bounded by a 30-second timeout. Error messages are compacted and limited before surfacing. Callers must not add PEM/private material to logs.

Required operations:

```text
genpkey
req
pkey
x509
verify
```

The target Ubuntu image must provide an approved OpenSSL version before deployment.

## Tests

Unit tests generate an isolated temporary CA and leaf certificate and cover:

- idempotent pending request reuse;
- private file modes for key, CSR, certificate, CA and metadata;
- rejection of broader file modes by the atomic state writer;
- metadata without private key/CSR;
- partial pending state failure;
- exact URI SAN validation;
- public-key match;
- atomic generation/active symlink;
- pending cleanup after activation;
- wrong identity rejection with pending state retained;
- certificate from another private key rejection;
- active symlink outside generations rejection.

## Remaining work in #27

This partial implementation does not close #27. Still required:

1. authenticated issuance/bootstrap/rotation API client;
2. certificate and CA response DTO handling;
3. persisted network lifecycle state and request idempotency;
4. dual bearer/mTLS mode behind explicit configuration;
5. no-silent-downgrade rule;
6. mTLS HTTP heartbeat/health transport;
7. revocation/recovery bootstrap;
8. systemd read/write path and installer review;
9. Agent integration tests for response loss/restart/conflict/expiry;
10. separate staging deployment and rollback acceptance.

## Safety invariants

This partial module does not:

- change the current Agent version/runtime;
- read current bearer credentials;
- call SaaS;
- issue a real certificate;
- change `install.sh` or systemd;
- enable commands;
- modify Xray, 3X-UI, nginx, routes, Exits, clients or subscriptions;
- deploy to `ru-spb1`.
