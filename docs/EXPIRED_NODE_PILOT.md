# Constrained expired-certificate pilot host capability

This helper implements the Node side of the separately approved seven-day pilot.
Installation remains disabled until accepted backups, the exact enrolled target,
machine host-key pins, SaaS standing approval and accepted immutable artifacts
have been independently admitted. Source tests are not runtime acceptance.

The central service has its own machine SSH identity. It cannot use an admin JWT,
Vault login, root ceremony, issuer token, shell, forwarding or PTY. The installer
only accepts an operator-supplied **public** Ed25519 SSH key; it creates no keys.

## Wire contract

The forced SSH command is exactly `inspect`, `prepare`, `deliver`, `apply` or `reconcile`.
There are no path/command/environment arguments. Each receives a bounded 64 KiB
JSON object on protected stdin. Required common fields are:

| Field | Meaning |
| --- | --- |
| `schemaVersion` | `1` |
| `operationId` | Prechosen canonical UUID v4, same as the durable SaaS operation |
| `hostBindingHash`, `nodeIdentityHash` | Exact immutable hashes from admitted root configuration |
| `expiredCredentialId` | Protected internal baseline identifier; never output |
| `certificateFingerprintHash` | SHA256 of leaf DER |
| `certificateSerialHash` | SHA256 of lowercase hexadecimal X509 serial; no blind zero stripping |
| `publicKeyHash` | SHA256 of SPKI DER, same as SaaS Node crypto |
| `certificateExpiresAt` | A timezone-bearing timestamp from authenticated SaaS baseline |
| `trustedCaBundleSha256` | SHA256 of the admitted local CA bundle bytes |
| `baselineObservedAt` | Authenticated baseline read time, at most 600 seconds old for fresh admission |
| `acknowledged`, `revoked`, `quarantined`, `compromised` | Respectively true, false, false, false; authenticated baseline evidence, independently enforced by SaaS |

All common fields remain identical for the operation, including the observation
timestamp. Unknown fields are rejected. New baseline observations do not mint or
replace an operation. SaaS admission separately proves issuer health, standing
approval, budgets, accepted backups and writer isolation.

`inspect` is read-only: it does not create/chmod TLS state or issue a CSR. It
proves active-selector confinement, root ownership and modes, leaf/key/metadata
consistency, actual SAN, serial/fingerprint/expiry match, currently valid CAs,
chain signatures, pending-triplet consistency and absence of unrelated ACK.
Only forensic signature verification uses `-no_check_time`; every CA validity
and the leaf notBefore are checked explicitly. Normal `active_identity` and
transport validation still reject expiry. Complete pending material is reused;
partial state is preserved and stops admission. Root/system writers require the
central maintenance exclusion; this mechanism cannot defend against arbitrary
concurrent root filesystem writes.

Successful inspection returns `ok`, `code=INTACT_EXPIRED`, `schemaVersion`,
`operationId`, `hostBindingHash`, `nodeIdentityHash`, `observedAt`,
`certificateFingerprintHash`, `certificateSerialHash`, `publicKeyHash`,
`identityUriHash`, `certificateExpiresAt`, `trustedCaBundleSha256`, and
`localRequestHash` (null when no CSR exists). It outputs no PEM, key, token or raw
tenant/Node/credential identifiers. The supervisor maps these fields to the
snake_case SaaS inspection contract and supplies its own proved backup/writer
facts; successful inspection is not proof of those central prerequisites.

`prepare` repeats expiry admission while holding the existing recovery flock,
then creates or reuses the local pending request and returns fresh inspection.
It releases that lock before any wrapper call. It creates a local pending key
and CSR, with no token/grant, SIGN, active-selector change or remote request.
The supervisor calls it before reserve. The resulting `localRequestHash`,
`pendingPublicKeyHash` and `pemRequestHash` are mandatory reserve bindings. The
pending public key is distinct from the expired leaf's `publicKeyHash`; comparing
these keys would incorrectly reject a normal recovery that uses a fresh key.
`pemRequestHash` is SHA256 of stripped, CRLF-normalized PEM plus its final newline.
Every successful protocol response also returns the verified
`helperSourceSha256` and immutable `configSha256` for the central pinned
artifact/config admission.

`deliver` adds `token` on protected stdin. The journal is durable before token
write, and identical delivery can resume with the same token hash while phase is
DELIVERED. It never replaces a token or re-delivers after apply dispatch. Accepted
operations are archived privately before a different current expired baseline
can be admitted. SaaS retains the permanent `(nodeId,expiredCredentialId)` budget
and dispatch guard; the Node archive is supplementary evidence.

`apply` adds no fields. It calls the existing recovery wrapper using hardcoded
paths, interpreter, service, backup and lock settings, with a clean environment.
The delivery lock serializes machine calls; the wrapper alone acquires the
existing recovery flock. It repeats expiry admission **inside that recovery
flock**, before stopping Agent. The wrapper's private forensic backup, atomic
activation, exact ACK and backup-token destruction remain in use.
The pilot delivery also checks the returned chain against the accepted fixed CA
bundle before activation, parses the actual signed certificate dates, checks
them against delivery metadata and requires remaining validity greater than
`rotateBeforeSeconds+3600`. It accepts only `CERTIFICATE_EXPIRED` delivery reason.
An insufficient or mismatched delivery retains the marker for reconciliation;
it never grants another SIGN.

The recovery client writes `post_dispatched_at` before the HTTP POST. Lost
response, process death or unbound legacy markers never grant a second POST,
even with the same CSR. Agent remains stopped on apply failure. Known credential
GET and idempotent ACK continue without SIGN. `retry_safe=yes` only means known
credential continuation, not permission for another issuer dispatch.
One fixed `recovery_already_running` wrapper rejection is proved before the
client/Agent-stop phase and may return the operation to DELIVERED after its child
has exited. Other missing-marker or unknown outcomes remain STOP. The helper
does not infer absence of side effects from a retryable network error.

`reconcile` resumes the exact protected operation. If the local marker lacks a
credential it additionally requires `credentialId`, `localRequestHash` (CSR DER
SHA256) and `pemRequestHash` (canonical PEM SHA256) from the authenticated internal
SaaS status CLI. Both hashes are checked independently; they are not equal by
definition. The receipt binds the existing marker, then uses GET/ACK. It does not
dispatch fresh POST. A committed private ACK receipt can restore only the same
operation's acknowledged cleanup marker after response loss. Known continuation
is allowed after pilot disable/expiry; compromise still stops it.

One positive pre-SIGN refusal may permit same-operation continuation: the exact
final HTTP409 `NODE_EXPIRY_PILOT_PRE_SIGN_REFUSED` must have delivered a validated
`no_sign_proof` durably stored locally. Fresh protected SaaS status must return
the identical receipt while RESERVED/no attempt, with `new_sign_allowed=true`.
Central passes it as `noSignProof` and `newSignAllowed`. Node compares operation,
authorization receipt identity, CSR DER/PEM and pending-key hashes, exact receipt
fields and bounded observation age, then repeats fresh expiry/window admission.
Token, authorization, operation and CSR remain the same. Transport loss or an
absent DB fence provides no proof, and a previous receipt cannot downgrade a
later unknown POST. This continuation requires current issuance authority;
known credential GET/ACK does not create another issuer dispatch.

Success is `ok=true,code=ACCEPTED,operationId,localRequestHash`. Failure is a fixed
sanitized STOP code. Neither stderr from OpenSSL nor wrapper output is exposed
through SSH. Central acceptance still requires three fresh heartbeats and a
normal renewal observation; the helper's success is local recovery proof.

Success additionally includes `cleanup` with `observed_at`, `operation_id`,
`credential_id_hash`, `local_request_hash`, `token_absent`,
`recovery_pending_absent`, `ack_pending_absent`, `backup_token_absent` and
`shadow_active`. These fields come from actual custody/identity/runtime/service
checks. The central service checks the credential hash against its protected
SaaS status receipt before passing the actual credential ID internally. A
successful wrapper tags its backup with the operation ID. The helper removes
remaining tokens only from those same-operation backups after proved durable
ACK and local acceptance, preserves the previous checksum receipt and updates
the manifest. An unrelated backup token is retained.

## Installation and custody

First install the accepted Node Agent baseline through its existing installer.
From the accepted source `agent` directory, the operator runs
`sudo /usr/bin/python3 install-expired-pilot.py` with a private JSON stdin envelope
containing `config`, `publicKey` and `centralAddress`. No secret may be placed in
argv. `config.sourceHashes` must already contain seven expected hashes from the
independently admitted source artifact. Before any mutation/program call, the
installer validates source pins, complete target/profile custody, fixed
root-owned non-writable parents, destination links/modes, accounts, keys,
sudoers, configuration and locks. An existing `wavemesh-expiry` account/group is
rejected unless a matching installation journal proves creation by this lineage;
UID/home/shell shape alone is insufficient.

One private lineage under `/var/lib/wavemesh-agent/expired-pilot-install` retains
exact previous source bytes/modes and `agent.env` under `before/`. A root0600
durable journal records PREPARING, STAGED, SOURCE_PROMOTING, SOURCE_PROMOTED,
ACCOUNT_CREATING, ACCOUNT_CREATED, CAPABILITY_PROMOTING and INSTALLED_DISABLED.
Staged files and sudoers syntax are verified before atomic source promotions.
Configuration stays disabled, and the Agent environment is not rewritten.
Source adoption requires the accepted maintenance exclusion; this installer
does not stop/start Agent.

ACCOUNT_CREATING is committed before the sole useradd dispatch. Readback requires
non-root UID/GID, the own system primary group and no supplemental groups, fixed
home/shell, and root-owned home/SSH directories. Unknown creation does not permit
another useradd; only a proved created row may continue. Five literal sudo
commands replace the wildcard capability. The root-owned, central-IP-restricted
forced-command authorized_keys is installed last after exact disabled
configuration/source/profile checks. Mode0644 allows sshd lookup while preventing
machine-user edits. Machine SSH uses `wavemesh-expiry` at the admitted IP, with
the exact HostKeyAlias.

Failures preserve backups, journal and phase. Re-running `mode=install` is
rejected. Protected `mode=status` reads status without advancing; `mode=reconcile`
with the identical envelope permits bounded readback/continuation. Changed
payload/source/custody, unresolved account creation or unrelated files stay STOP.
The latest journal is re-read after the retained installation flock, so concurrent
reconciliation cannot overwrite a newer receipt. Terminal receipt includes
source/config/key/sudoers hashes. No rollback/delete of old material or replacement
of unrelated capability is automatic.

The installer does not configure sshd, generate keys, connect to hosts or activate
the pilot. Actual sshd/PAM login for the new locked account, sudo enforcement and
host-key custody remain separate runtime acceptance facts.

`config` uses `schemaVersion=1`, `enabled=false`, `compromised=false`, `activatedAt`,
`expiresAt` (at most seven days apart), fixed `tenantId`, `nodeId`, `externalId`,
`environment`, `hostBindingHash`, and `nodeIdentityHash`. Admitted expected
`sourceHashes` cover helper, recovery client, expired inspector, mTLS state,
node_agent, recover.sh and the forced-command script. Runtime root configuration
must never accept client-selected paths or hashes. Enable and revoke are explicit
operator mutations within the admitted functional window.

`configSha256` is SHA256 of canonical JSON (sorted keys, compact separators,
UTF8 without ASCII escaping), excluding **only** `enabled`. Formatting and
disable/revoke cannot invalidate known-credential continuation; target, trust,
profile, source, pilot times and `compromised` remain pinned. `compromised=true`
always stops all host calls. The installer also records `configBytesSha256` for
exact file custody of the disabled installation receipt.

Root configuration also requires `trustedCaBundleSha256`, the accepted trust
bundle pin. A client-supplied baseline cannot choose a different CA bundle. An
accepted CA change requires deliberate root configuration and central pin
admission; it does not silently expand the pilot trust boundary.
The accepted current profile is pinned as `certificateTtlSeconds=86400` and
`rotateBeforeSeconds` equal to the actual Agent environment (default21600).
Fresh inspection requires every CA to cover the full 24-hour leaf horizon plus
one hour of delivery buffer and exposes the minimum `trustedCaValidUntil`. Backend
pre-SIGN admission independently checks its configured issuer/CA/profile horizon.

Private files live in `/etc/wavemesh-agent`: `expired-pilot.json`,
`expired-pilot.operation.json`, `expired-pilot.delivery.lock`, `recovery.token`,
`recovery.pending`, and `recovery.completed.json` (all mode0600, root-owned).
Archived accepted operation receipts remain under `expired-pilot.operations/`
(mode0700). The TLS root and generation/pending directories must already be
root-owned mode0700. Recovery backups remain under the existing private backup
root. Failed receipts and encrypted references are retained for reconciliation.
Disable does not remove journals, old generations or the recovery receipt.

## Proof and rollback

`tests/unit/test_expired_node_pilot.py` contains fixed-command negatives and native
Linux root/OpenSSL fixtures for actual expired leaf, expired CA, SAN/baseline/key
mismatch, pending key/CSR mismatch, permissions/owner/symlink rejection and strict
normal identity. CI invokes this suite with sudo so ownership cases cannot be
reported as Windows skips. Existing recovery tests prove one POST on unknown
outcome, known-delivery GET, lost ACK replay and committed partial cleanup.

`test_expired_pilot_installer.py` executes isolated custody fixtures with
simulated account lookup/programs. It proves rejection before mutation, exact
backup preservation, partial promotion/key failure, disabled capability,
known user-creation reconciliation, no retry after unknown creation, unmanaged
account rejection and unexpected-group STOP. Native ownership/hardlink proof
runs with sudo in CI. No fixture creates a real account or SSH machine key.

Once an operation is dispatched, rollback to a recovery client without the
durable guard is unsafe. Disable issuance and retain compatible helpers/journals;
use forward fixes or exact known-credential reconciliation. Unresolved outcome
does not authorize deleting state, replacing a token, resetting the selector or
signing again. Runtime activation and Linux CI results must be reported separately.
