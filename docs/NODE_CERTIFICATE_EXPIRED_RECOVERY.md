# Node expired-certificate recovery

Status: source contract; staging acceptance required.

## Purpose

`CERTIFICATE_EXPIRED` is the break-glass recovery reason for a Node whose previously acknowledged mTLS certificate has expired while the local private key still exists, still matches that certificate, and still represents the expected tenant/Node SPIFFE identity.

This is not a lost-key or compromised-key procedure. If the private key is missing, use `LOST_KEY`. If compromise is suspected or proven, use `COMPROMISED_KEY`; do not use certificate expiry to bypass the compromised-certificate issuer-revocation contract.

## Required preflight evidence

Before an operator creates the short-lived SaaS recovery authorization, prove read-only that:

- the Agent is installed from the expected source/version;
- the target tenant/Node identity is known without printing secret material;
- the active local certificate is expired;
- the local private key still matches the expired certificate;
- the certificate carries exactly the expected tenant/Node SPIFFE URI;
- a complete `tls/pending/client.key`, `client.csr`, and `metadata.json` triplet is present when one already exists;
- there is no unrelated pending certificate acknowledgement or conflicting recovery transaction;
- there is no evidence of private-key compromise.

SaaS independently enforces the commercial/control-plane classification: no still-valid active mTLS credential may exist and an acknowledged, non-revoked expired mTLS credential must exist for the exact Node. Direct recovery repeats the no-valid-credential check to reject a stale authorization if normal rotation recovered concurrently.

## Pending request preservation

The recovery client uses `NodeMtlsState.prepare_pending_request()`.

A complete existing pending key/CSR/metadata triplet is reused. Do not delete it or generate another CSR simply because the active certificate expired. A partial or mismatched pending request fails closed.

This preserves the existing lost-response guarantee: an ambiguous recovery POST retries the same CSR/request hash rather than implicitly creating another certificate request.

## Server response contract

The recovery client accepts recovery metadata only for the explicit SaaS reasons:

```text
LOST_KEY
COMPROMISED_KEY
CERTIFICATE_EXPIRED
```

Unknown non-null `recovery_reason` values fail closed before local certificate activation. The reason is metadata only on the Node side; it does not relax certificate, delivery-expiry, key, CA, SPIFFE, ACK, or replay validation.

Break-glass recovery must not retain an overlap window: `previous_valid_until` remains required to be null.

## Controlled recovery boundary

The actual recovery remains a separately authorized runtime mutation. It requires one short-lived, node/tenant-scoped `wvr_` authorization stored as a root-owned mode-0600 file and never passed on the command line or printed.

The existing wrapper contract remains unchanged:

1. acquire the host recovery lock and create the restricted rollback/forensic backup;
2. validate local recovery state;
3. stop the Agent before certificate mutation;
4. reuse the persisted CSR when present;
5. perform the direct recovery POST/GET sequence;
6. validate the returned certificate against the local private key, CA and exact SPIFFE URI;
7. atomically activate the recovered generation;
8. ACK the exact recovered credential;
9. clear the working authorization and recovery markers only after acknowledged recovery;
10. start the Agent and require `SHADOW_ACTIVE` plus fresh central heartbeats before acceptance.

Do not manually delete `runtime.json`, reset `BLOCKED`, change the active selector, regenerate the pending CSR, or use the old expired identity as a rollback after SaaS has accepted the recovery request.

## Failure and retry behavior

On an ambiguous POST, GET, or ACK outcome, preserve the current recovery token and persisted request/credential state and rerun the same recovery transaction. Do not create another authorization or CSR unless the existing transaction is conclusively invalidated according to the recovery contract.

If the wrapper cannot prove a safe continuation, leave the Agent stopped and keep the preserved state for read-only reconciliation. Do not auto-repair by deleting state.

## Staging acceptance

Source/CI proof is not runtime acceptance. A controlled staging expiry recovery must prove:

- the actual expired certificate and intact key/SPIFFE binding;
- reuse of the pre-existing pending CSR when present;
- exactly one scoped `CERTIFICATE_EXPIRED` authorization;
- successful recovery delivery and ACK;
- recovered certificate/key/CA/SPIFFE validation;
- Agent transition to `SHADOW_ACTIVE`;
- fresh mTLS heartbeats visible in SaaS;
- cleanup of `recovery.token`, `recovery.pending`, and pending ACK state according to the wrapper contract;
- preservation of immutable old certificate generations for forensic review;
- no recovery token, CSR, PEM, private key, Node credential or subscription secret in logs.

Do not proceed to application SIGN-token activation while Node mTLS remains `BLOCKED` or central heartbeat freshness is not proven.
