# Disabled Entry replacement preparation

This is a Node executor prerequisite, not a complete migration workflow.
`access_replacement_prepare_v1` is advertised only when mTLS command execution
is ready. Existing provisioning and credential replacement retain their
enabled-only contract.

The allow-listed `access.prepare_replacement` command uses the existing access
payload plus a required safe `replacement_id`, with `enabled` strictly false.
The durable access fence binds the operation ID and normalized desired state.
An identical retry reuses persisted identity, including after a lost create
response; a different operation at the same version is rejected. A later
operation needs a newer version from SaaS.

The panel receives disabled absolute entitlements, which must be read back
before a receipt is returned. Disabled preparation does not require live
subscription links and never cleans up an existing Entry. Secret material goes
only to `internal/v1/nodes/{node}/replacements/{replacement}/materialize`, over
the existing authenticated channel. It is not posted to the ordinary access
material endpoint and is not a published user subscription. A missing or failed
operation endpoint fails command completion; there is no fallback endpoint.

SaaS integration is still required before issuing this command: a durable,
idempotent replacement operation must bind tenant, access, source and target,
expected version and request hash. Its receipt endpoint must authenticate the
target, recheck ownership and paid state, and retain candidate material separate
from current published material. Capability negotiation alone authorizes none
of these business transitions.

Activation, atomic cutover, cancellation/expiry/refund cleanup, unreachable-old-
node cleanup debt, rollback, admin UI and actual VPN traffic acceptance remain
unimplemented by this change. No staging or production acceptance is claimed.
