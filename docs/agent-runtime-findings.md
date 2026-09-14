# Advisory runtime client findings

The optional Agent collector provides private evidence for a later SaaS-owned
quarantine decision. It has **no disable, delete, rotate, provisioning or panel
write operation**. SaaS remains responsible for ownership and desired state.

`WAVEMESH_AGENT_RUNTIME_FINDINGS_MODE=disabled` is the default and installer
migration value. `observe` requires primary `WAVEMESH_AGENT_AUTH_MODE=mtls`, a
configured shadow mTLS lifecycle and a currently active certificate. Bearer and
bootstrap modes cannot enable this feature. Duplicate finding settings are
rejected. SaaS must separately enable its `NODE_RUNTIME_FINDING_INTAKE_ENABLED`
gate and support a `report_sha256`-bound receipt; the older unbound response is
insufficient. Do not enable either side before deploying and testing both.

## Collection and limits

After the ordinary heartbeat, a supervised Python child collects at most one
candidate. A 20-second process timeout bounds stalled panel reads; the child has
no SaaS transport. stdout/stderr are discarded by the Agent. Errors produce only
a fixed status, never exception text. The daemon separately delivers already
journaled reports using its existing mTLS client. Ordinary health and access
command processing retain their existing contracts; no command type is added.

Collection holds the shared `/run/lock/wavemesh-node.lock` and rejects unresolved
CLI transactions before reading config/inventory/panel state. A separate private
journal lock excludes concurrent collectors/senders. Delivery releases the Node
lock and keeps the journal lock. Lock inodes are never replaced or truncated.

Only existing GET endpoints are allowed: `/panel/api/clients/list`, exact encoded
email lookup at `/panel/api/clients/get/…`, and `/panel/api/inbounds/list`. Redirects
are refused, each response is limited to 2 MiB and each socket read has a three
second timeout. Subscription URLs are never fetched. The client list must have
unique case-insensitive emails and at most 512 entries. Local access JSON is
bounded to 2048 files/16 MiB and must exist, be private and contain valid JSON;
missing storage is not treated as proof of no managed accesses.

One candidate per cycle is selected with a durable round-robin cursor and at
least 60 seconds between scans. Known local email/UUID/subId references in Agent
state or Node config exclude the candidate. The selected client must be enabled,
have a valid identity and belong only to public VLESS inbounds. Repeated reads
must agree on selected client data, client names, config and access inventory.
These checks do **not** fence UI/legacy/manual panel writers or prove SaaS
ownership, uniqueness of credentials in all other clients, or Xray traffic state.
They establish an advisory observation only; they cannot authorize quarantine.

## Durable private evidence and delivery

`/var/lib/wavemesh-agent/runtime-findings` is root-owned, mode 0700, under the service's
existing write allowance. Files are mode 0600, regular, single-linked and symlinks are
rejected. `WAVEMESH_AGENT_RUNTIME_FINDINGS_STATE_ROOT` is an operator configuration
override, not a remote command argument. The collector does not recursively
chmod/reown existing unsafe storage. Installer manages the directory/module but
never starts observation or rewinds/deletes its journal during code rollback.

Each revision file stores node/tenant scope, the complete selected client record,
public inbound IDs, canonical config/access-inventory/client-list digests, a
private random 32-byte nonce, the exact report and delivery state. Only six fields
leave the node: schema_version 1, advisory kind, independent UUIDv4 finding_ref and
revision, salted baseline_sha256, and observed_at. These UUIDs are opaque evidence
locators, never VPN client credentials. Local records and private hashes must not
be pasted into logs/issues or used as public identifiers. Reports contain no
client email, credential UUID, subId, subscription URL, token or full baseline.

Atomic file replacement follows file fsync and directory fsync. Baseline/report
fields cannot be changed through the journal API. `IN_FLIGHT`, attempt number and
retry deadline are committed **before** POST. Timeout, process death and ACK-save
failure retain the same envelope; replay never generates a new client/ref/revision
or observation time. Backoff is 30–900 seconds and at most 8 attempts, persisted across
restarts. Definitive non-transient 4xx or exhausted retries require review with
evidence retained. A stale/conflict response never silently refreshes or replaces
the original report. Recovery of blocked delivery is a separate explicit step.

Acceptance requires 202 and an exact receipt with accepted=true, finding_id,
disposition=OBSERVED_ONLY, the original observed_at and matching report_sha256.
The hash is SHA256 over compact sorted-key UTF-8 JSON of the six report fields.
Missing hashes and reordered receipts—even with equal timestamps—are rejected.
Receipt parsing alone does not establish panel or traffic quarantine.

Unchanged accepted baselines are not re-reported or timestamp-refreshed. A changed
baseline produces another immutable revision under the same local finding_ref.
Future quarantine must request fresh evidence explicitly and revalidate all
ownership/runtime conditions; this collector does not maintain a perpetual
freshness lease. Storage stops at 128 revision files or 16 MiB; it never silently
deletes evidence. Retention/recovery policy must be proven before production.

Heartbeat exposes only fixed finding state/counts. Journal limits, unsafe state
or read failure produce COLLECTION_BLOCKED; delivery failure yields RETRY_PENDING
or REVIEW_REQUIRED. Default-disabled mode does not read panel or journal state.

## Remaining acceptance

Synthetic tests cover permissions, links/FIFO, private hashes, changed or corrupt
evidence, local membership, observable drift, lock exclusion, lost responses,
crash before/after dispatch, ACK/disk failure, durable retry bounds, reordered
receipts, disabled/auth gates and installer/rollback preservation. They do not
prove live acceptance. Still required: SaaS ownership/lifecycle fencing, an atomic
desired quarantine command, explicit exclusion of unfenced runtime writers,
disable-only reconciliation, actual VPN rejection, healthy controls and session
drain. Rolling source back must never reactivate a quarantined client.
