# Node credential recovery

Status: source contract ready; staging acceptance still required

## Purpose

Recover an existing WaveMesh Node Agent when its current mTLS private key/certificate can no longer be used. Recovery is explicitly authorized by a short-lived, single-use, tenant- and Node-scoped `wvr_` authorization created in SaaS by an operator.

Production recovery does **not** create or install a temporary bearer credential. The existing `WAVEMESH_AGENT_TOKEN` is not changed by this operation.

## Installed tools

The Agent installer provides:

```text
/usr/local/lib/wavemesh-agent/node_recovery.py
/usr/local/sbin/wavemesh-node-agent-recover
```

They are inert unless a private recovery authorization is present, except for finishing local cleanup after SaaS has already acknowledged a recovery.

## Secret handling

Install the one-time authorization as:

```text
/etc/wavemesh-agent/recovery.token
```

Required properties:

- root-owned regular file;
- mode `0600`;
- contains only the single `wvr_` value;
- not a symlink;
- never supplied as a command-line argument;
- never printed by the recovery client or wrapper.

The replacement ECDSA P-256 private key is generated locally by `NodeMtlsState`. It never leaves the host. The CSR is sent only to the direct recovery endpoint and is never written to logs or JSON recovery markers.

The mode-0600 `recovery.pending` marker contains only bounded identifiers, request/public-key hashes, timestamps, and—after SaaS has accepted the CSR—the recovered credential ID. It contains no authorization, CSR, certificate, chain, or private key.

## Server contract

The recovery client uses the SaaS direct CSR contract:

```text
POST /internal/v1/nodes/recover/certificates
GET  /internal/v1/nodes/recover/certificates/:credentialId
POST /internal/v1/nodes/recover/certificates/:credentialId/acknowledge
```

All three calls use the same `wvr_` authorization.

The POST body is exactly the local tenant ID, Node ID, and persisted CSR. No bearer replacement token is requested or sent.

## Controlled command

Set only the non-secret external Node ID and run:

```bash
WAVEMESH_RECOVERY_EXTERNAL_NODE_ID=ru-spb1 \
  /usr/local/sbin/wavemesh-node-agent-recover
```

The external Node ID remains an operator-facing local label for the controlled wrapper; authorization scope is enforced by SaaS using the internal tenant and Node IDs.

The wrapper:

1. acquires a host lock;
2. validates the Agent environment and private recovery state;
3. creates a restricted backup of Agent environment and TLS state;
4. runs a secret-free recovery preflight;
5. stops `wavemesh-node-agent.service`;
6. generates or reuses one persisted local P-256 key + CSR;
7. submits the CSR through the direct recovery API;
8. persists the returned credential ID before certificate activation;
9. records pending ACK state before switching the active identity;
10. validates the returned certificate against the local private key, CA bundle, and exact SPIFFE URI;
11. atomically installs/switches the recovered certificate generation;
12. ACKs the same credential with the same recovery authorization;
13. removes the working recovery authorization only after successful ACK;
14. starts the Agent and waits for `SHADOW_ACTIVE`;
15. destroys any private backup copy of the recovery authorization only after full acceptance.

Old immutable certificate generations remain on disk for forensic review, but SaaS recovery semantics revoke/supersede the prior active certificate according to the operator-selected recovery reason.

## Crash and retry safety

Before the first network request, `NodeMtlsState` persists:

```text
tls/pending/client.key
tls/pending/client.csr
tls/pending/metadata.json
```

and the recovery client writes `recovery.pending`.

The pending key/CSR are reused verbatim. The client never silently creates another CSR while the marker exists.

Important retry boundaries:

- **POST timeout / lost response:** the same CSR is POSTed again. SaaS binds the consumed authorization to the deterministic CSR request hash and returns the same recovered credential for an exact replay.
- **Credential ID persisted but delivery not activated:** restart performs GET for that exact credential ID, then validates and atomically activates the certificate.
- **Certificate activated but ACK response lost:** the persisted acknowledgement and recovery marker cause restart to repeat only the ACK for the same credential.
- **ACK accepted but the local process crashes during cleanup:** the marker records the acknowledged state before cleanup, so the next run can finish local cleanup without issuing a new certificate.
- **Marker/CSR hash mismatch, partial local key/CSR state, unrelated pending ACK, or legacy temporary-bearer recovery marker:** fail closed; do not regenerate implicitly.

For `COMPROMISED_KEY`, SaaS may return a retryable failure until issuer revocation of every affected prior certificate is complete. The Agent preserves the same authorization/CSR state and retries the same recovery transaction.

## Failure behavior

When a network or remote operation is ambiguous before ACK, the wrapper:

- leaves the Agent stopped;
- preserves the one-time authorization and pending key/CSR state;
- preserves the private backup needed for the same retry;
- prints only a bounded diagnostic path/status;
- does not print the authorization, CSR, certificate, chain, or private key.

Do not restore the old TLS active selector after SaaS has accepted break-glass recovery. Re-run the recovery command with the retained state so it retrieves/ACKs the same credential.

## Acceptance

Source tests prove only the deterministic local/network contract. Runtime acceptance is separate.

A controlled staging run must end with:

```text
NODE_SCOPED_CREDENTIAL_RECOVERY=PASS
```

and prove:

- Agent service is active;
- mTLS runtime reaches `SHADOW_ACTIVE`;
- the recovered credential is ACKNOWLEDGED;
- the working and backup copies of the one-time authorization are destroyed;
- `recovery.pending` and pending ACK state are cleared;
- the existing bearer value was not replaced by recovery;
- no recovery authorization, CSR, certificate/chain body, private key, or subscription data appears in logs;
- old certificate generations remain available for forensic review.

Central staging acceptance must additionally prove fresh mTLS heartbeats, LOST_KEY and COMPROMISED_KEY procedures, replay/wrong-scope rejection, and the required mTLS-only observation window. CI/source evidence alone does not satisfy those checks.
