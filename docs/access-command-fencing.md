# Durable access command fencing

Provisioning, entitlement updates and replacement cleanup take a nonblocking OS lock per access. Contention fails before panel calls; process death releases the lock. On Linux this uses flock. Keep the state directory on the Node's local durable filesystem, not a shared network mount.

Before a panel mutation, the executor atomically persists the highest admitted desired version and SHA256 of the normalized operation/entitlements. File and directory fsync precede panel work on Linux. Replays must match that version and payload. Lower versions are rejected even if the newer command failed after admission; reconcile the newer desired state instead of reverting it implicitly. Panel verification still determines success, so a fence alone is not evidence of materialization.

Existing identity state files also contribute to the maximum version. A same-version legacy command without a recorded payload hash is rejected because its original parameters cannot be proven. Reconcile the outstanding command before upgrade, or issue a new SaaS-owned desired version; do not delete state or synthesize a hash to bypass this check. Higher-version updates can preserve the existing identity.

Cleanup holds the same lock and rejects stale versions. It skips older records sharing the current panel identity, as entitlement updates intentionally preserve that identity.

The placement executor also supports absolute disabled entitlements, including a tombstone without a known identity. Its all-identity inventory, material and successful result are separate required proofs. The fence prevents local replay from reverting a newer locally admitted command; it cannot know about a newer SaaS desired version that the Node has not received. Control-plane leasing/reconciliation must handle that distributed race. Source and synthetic tests do not establish live VPN or whole-system acceptance.

Acceptance tests cover identical replay, payload conflict, stale provisioning/update/cleanup, failure after fence persistence, legacy-state migration, identity-preserving cleanup, concurrent processes and lock release after process death. Existing entitlement tests cover panel lost responses. This source change does not deploy or enable commands on a live Node.

## Placement local fence capability 3

The schema 2 wire command and per-access lock/path stay unchanged. The current
V2 durable fence uses `placement_payload_hash`, without a legacy `payload_hash`.
The actual pre-placement executor requires the latter field. It therefore
refuses the guarded V2 fence before update, provision or cleanup, even with a
larger requested version. Extra generation/revision fields alone did not provide
that property: the old executable ignored them. Tests retain the exact old
`dc4e66d` source and SHA-256 in `tests/fixtures/access_runtime_dc4e66d.json`, and
exercise it in a separate process against the new file.

V1 files retain their original shape. A higher V2 command may replace a legacy
fence under the existing lock, uncertainty/quiescence and identity rules. A
same-version legacy record cannot be given an invented replay hash. Older,
preparation-only V2 files with the legacy-compatible hash are refused and kept
intact for reconciliation; there is no implicit migration or deletion of that
unknown state. Production V2 admission remains closed until the whole transition
is qualified. No accepted production V2 state or runtime grant is assumed here.

The installed runtime advertises `placement_fence: 3`; the paired Agent requires
that exact capability and the existing unknown-effect barrier. An old Agent or
old runtime cannot be substituted for the qualified pair. Genuine in-flight
panel requests still require the existing POSIX lock and service quiescence
proof. This field change does not cancel an HTTP request already accepted by
the panel, exclude a writer using another state directory, or authorize runtime
upgrade, bootstrap, SIGN or cleanup. Admission must bind the existing durable
state directory and compatible artifacts before enabling V2.
