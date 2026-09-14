# Disabled Entry replacement preparation

This is a Node executor prerequisite, not a complete migration workflow.
`access_replacement_prepare_v1` is advertised only when mTLS command execution
is ready. Existing provisioning and credential replacement retain their
enabled-only contract.

The target panel must provide the authenticated `clients/addDisabled` extension
described in [the pinned backend contract](../third_party/3x-ui/README.md).
Upstream 3X-UI 3.4.2 `clients/add` forces false to true, and its client-record
INSERT also defaults to true. An expired timestamp and a subsequent disable do
not close the activation window. The Agent never falls back to that endpoint
for disabled creation. Its command capability is not proof that the installed
panel supports the extension: panel artifact deployment and acceptance remain
prerequisites before SaaS may issue preparation commands.

The allow-listed `access.prepare_replacement` command uses the existing access
payload plus a required safe `replacement_id`, with `enabled` strictly false.
The durable access fence binds the operation ID and normalized desired state.
An identical retry reuses persisted identity; a different operation at the same
version is rejected. A lost create response leaves the durable panel-request
barrier pending and requires reconciliation before another mutation. A later
operation needs a newer version from SaaS.

The panel receives disabled absolute entitlements through `addDisabled`, which
must be read back before a receipt is returned. An enabled client is a contract
violation and is not silently disabled and then accepted. Disabled preparation does not require live
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
