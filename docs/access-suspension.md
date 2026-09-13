# Suspension and restoration executor contract

An access-capable Agent advertises `access_entitlements_v2=true` when command execution is ready. SaaS must require this capability before emitting disabled entitlement commands. This contract uses the existing allowlisted `access.update_entitlements` with a strict boolean `enabled`; initial provision and credential replacement still require true.

False updates the existing panel identity with an absolute disabled state and verifies identity, attached inbounds, enabled state, expiry and limits by readback. A lost write response is reconciled by readback. No additive bulk fallback is attempted for suspension. Empty subscription links do not invalidate a verified disabled state. The returned material identifies the retained credential; it does not mean the access is enabled.

Restoration is a higher desired version with enabled=true. It retains identity and verifies active entitlements and subscription links. Durable command fencing covers both enabled and disabled updates, so an older disable cannot revert a locally admitted restoration. SaaS must project enabled/status from its matching desired version and reject stale receipts; the Agent cannot see desired versions not yet delivered to it.

Deploy alongside a compatible SaaS receipt handler. Do not enable managed-expiry reconciliation until capability checks, paid-period rechecks and desired-version concurrency control are implemented. Existing historical same-version state without a payload fence requires reconciliation as documented in access-command-fencing.md.

Tests prove disable/replay/restore, stale disable rejection, lost response readback, ignored enable-field failure, no additive suspension fallback, empty links while disabled and links required on restoration. These are source tests with a panel fake. Real Xray traffic cessation/restoration, process/Node faults and client reconnection still require isolated staging acceptance.
