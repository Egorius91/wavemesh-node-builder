# Durable access command fencing

Provisioning, entitlement updates and replacement cleanup take a nonblocking OS lock per access. Contention fails before panel calls; process death releases the lock. On Linux this uses flock. Keep the state directory on the Node's local durable filesystem, not a shared network mount.

Before a panel mutation, the executor atomically persists the highest admitted desired version and SHA256 of the normalized operation/entitlements. File and directory fsync precede panel work on Linux. Replays must match that version and payload. Lower versions are rejected even if the newer command failed after admission; reconcile the newer desired state instead of reverting it implicitly. Panel verification still determines success, so a fence alone is not evidence of materialization.

Existing identity state files also contribute to the maximum version. A same-version legacy command without a recorded payload hash is rejected because its original parameters cannot be proven. Reconcile the outstanding command before upgrade, or issue a new SaaS-owned desired version; do not delete state or synthesize a hash to bypass this check. Higher-version updates can preserve the existing identity.

Cleanup holds the same lock and rejects stale versions. It skips older records sharing the current panel identity, as entitlement updates intentionally preserve that identity.

This is one prerequisite for suspension/restoration. Disabled entitlements remain unsupported. SaaS receipt semantics, version/capability negotiation, managed expiry scanning, unreachable-node cleanup and isolated runtime acceptance remain outstanding. The fence prevents local replay from reverting a newer locally admitted command; it cannot know about a newer SaaS desired version that the Node has not received. Control-plane leasing/reconciliation must handle that distributed race.

Acceptance tests cover identical replay, payload conflict, stale provisioning/update/cleanup, failure after fence persistence, legacy-state migration, identity-preserving cleanup, concurrent processes and lock release after process death. Existing entitlement tests cover panel lost responses. This source change does not deploy or enable commands on a live Node.
