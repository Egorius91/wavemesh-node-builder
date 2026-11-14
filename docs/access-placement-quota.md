# Allocation-bound placement commands

Status: local schema3 executor/transport implementation. SaaS reservation,
database guards, final counter qualification and real engine/native acceptance
are still required. This document does not enable finite or V2 admission.
Existing schema1/2 paths keep their existing contract until a local schema3
fence is established. Stock 3x-ui quota overrun is accepted by the owner; Q
bounds issued portions, rather than an exact dataplane byte cutoff.

## Command and material

Schema3 extends the existing authenticated placement envelope. Its exact
payload adds one required `traffic_budget`, either null or this object:

| Field | Meaning |
| --- | --- |
| epoch_id | UUIDv4 of this reserved meter epoch |
| baseline_receipt_id | SaaS aggregate initial-usage receipt |
| credit_receipt_id | Immutable latest credit receipt |
| credit_revision | Positive contiguous revision within the epoch |
| counter_baseline_bytes | Canonical decimal counter at epoch start |
| counter_identity_hash | SHA256 identity of an existing counter, or null for a new identity |
| counter_source_receipt_id | Qualified baseline/settlement material, or null with a new identity |
| credit_bytes | Positive cumulative credit for this epoch, not a delta to add again |

The commercial `quota_bytes` stays the common cap. The local panel receives
the absolute `counter_baseline_bytes + credit_bytes`, checked within int64.
Retries never add that amount to a panel counter or reset traffic. Null budget
cannot enable finite access. An explicit unlimited transition requires null
budget, commercial quota0 and no still-open local credit epochs.

Material retains the existing profile/deny shape and adds exact budget echo,
boolean `quota_history_complete` and `traffic_counters`. Each counter has only
`identity_hash`, `up_bytes` and `down_bytes`; byte quantities are canonical
unsigned decimal strings. The identity is SHA256 of UTF-8
`JSON.stringify([panel_email, client_uuid, sub_id])`. Counters do not export those
underlying credentials. Enabled material must include its own identity and
usable credit. Disabled material covers the entire retained identity count.
The panel's traffic reply must match email, uuid and subId from the retained
credential before its counters are hashed or recorded. A matching email alone
does not bind a counter after another writer changed the underlying client.
This response shape is traced to pinned stock v3.11.0
[ClientTraffic](https://github.com/MHSanaei/3x-ui/blob/v3.11.0/internal/xray/client_traffic.go)
and its [lookup](https://github.com/MHSanaei/3x-ui/blob/v3.11.0/internal/web/service/inbound_traffic.go).
An incompatible/missing response remains unknown; these source links do not
qualify an installed panel or enable admission.

These are panel-observed counters, not a final traffic settlement proof.
SaaS must independently qualify initial usage, deny/final counters and the
matching successful command before returning unused credit. Late/pending
traffic and engine persistence must be assessed on the exact installed stack.

## Durable execution and failure

The existing atomic per-access fence owns schema3 budget history; no second
meter file or new runtime service is introduced. `quota_epochs` retains every
credit, bound identity and observed up/down high-water marks. Its `terminal`
flag means the local deny was observed, not that SaaS settled the reservation.
The same fence's `quota_counters` retains every observed identity and both
counter high-water marks before any credit epoch exists. A higher command
cannot forget an orphan, accept a lower counter or recreate a missing known
identity. Enabled work checks these observations before panel mutation; a deny
still needs complete retained counters before it can report accounting proof.
A deny referring to a previously unknown credit epoch still disables identities
but permanently marks its local history incomplete. Receiving the reservation
ID cannot establish that its earlier effects or counter history survived.
New credit revisions strictly increase the same absolute allowance; a new
epoch cannot enable while an earlier local epoch is still open. Rotation also
requires all previous identities disabled before the new identity can apply
credit. Restore can continue the same identity with a new reserved epoch and
the proven counter baseline.

Counter rollback, absent counter history, automatic reset and disappearance
of a bound client fail closed. Recreating that client would refill its credit,
so a missing identity cannot be recreated by an ordinary grant retry. A lost
create reply is reconciled against the same durable identity after the existing
unknown-effect barrier; it does not mint another client.

If the fence is lost while identity state remains, a higher deny can still
run, but material retains `quota_history_complete=false`. It cannot enable
again or silently promote that state into verified accounting history.

A completed command stores its exact material and checksum in the same fence
before returning it. A same-command retry returns that immutable receipt with
no panel I/O, even after quota exhaustion. It is historical application proof,
not a fresh runtime readback. Fresh observations require a later command;
SaaS must not overwrite the earlier material receipt with changing counters.

Routine schema3 replacement cleanup retains the disabled old clients/counters
and returns0. Deleting them requires a separately qualified accounting cleanup
contract. State/history and outstanding reservations are not deleted on rollback.

## Compatibility and proof

The pinned pre-budget executable6228bb7 is retained under tests/fixtures with
its source commit and SHA256. It rejects schema3 on first dispatch and rejects
a stored schema3 fence even for a higher schema2 local version. The new Agent
recognizes the old runtime for schema2, but requires the exact schema3 budget
capability for schema3. Its read-only probe is cached by actual artifact bytes.
After budget initialization rollback keeps forward reconciliation; downgrading
writers cannot remove the protocol marker or reissue a whole common Q.

`python3 tests/unit/test_placement_quota.py` exercises the budgeted executor and
Agent transport with synthetic panel/counters and durable local files. The
unit fixture mocks native service quiescence; it is not Linux/systemd, stock
3x-ui/Xray, Node, VPN or commercial acceptance. Native and hosted qualification
must use current source and the exact paired SaaS contract before admission.
