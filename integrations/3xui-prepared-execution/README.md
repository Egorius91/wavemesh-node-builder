# Private 3x-ui execution attestation checkpoint

This is an unpublished, unqualified source checkpoint for PR4 stage1. Apply the
accompanying patch only to upstream 3x-ui v3.11.0 commit
`c621ba9014a6aa21fd7c9ecf45655e44e24ebf13`. Preserve upstream GPL-3.0 licensing.
The source manifest pins every changed file and the immutable local commit.

The existing Process/native API owner holds a consumed cold generation and
accounts subsequent native effects. Xray 26.10.21 stays unchanged, with SHA256
`a27e3f95ab6ce270956d0cecd32eda4686a811afc71c64a6a734a3dede14883e`.
Managed Linux mode is explicitly opted into with `XUI_PREPARED_ATTESTATION=1`;
this package changes no installed service or live flag. Default behaviour and
outbound-test processes remain unmanaged and cannot issue an attestation.

The managed launch seals config and the exact stock executable in Linux memfds.
It starts the core from inherited sealed descriptors, pins actual executable and
process identity, and requires an actual native API response. Control is a
filesystem Unix socket at `/run/wavemesh-xray/core.sock`; the read-only snapshot
is `/run/wavemesh-xray/snapshot.sock` in the same producer process. The parent is
root-owned 0700, both sockets 0600, and Unix peer credentials are checked. Managed
control has no TCP fallback. Existing normal callers use explicit API endpoints.

All eight native mutators invalidate the view before effects under one owner
mutex. Complete supported success can advance the attested view; errors, lost
responses, unsupported effects, process changes, persistence failure and service
hot/partial paths stay UNKNOWN. `SetConfig` cannot qualify a shadow. Service hot
apply is deliberately conservative and needs a new cold generation; selector-only
CAS is a separate owner-local method with no public mutating endpoint.

Private execution snapshots and path observations use version 3. Every new sample
must bind equal before/after/independently-current generation and epoch. A→B→A
cannot reuse an earlier sample. Only a bounded, complete chain of native selector
receipts for the one owned unconditional rule can preserve hysteresis. Cold
launch resets prepared selectors to blackhole. Public channel v1 is unchanged.

Supported attestation input is intentionally bounded: VLESS/XHTTP/TLS/stream-one
managed ingress, inline inputs, canonical integral JSON numbers and the reserved
private native API boundary. External certificate/script/geodata files, geoip or
geosite references, environment expansion, fractional/exponent numbers and other
ingress combinations fail managed admission. This is a material prerequisite;
existing stock templates are not assumed to satisfy it. Unmanaged configurations
retain stock operation. A later scope must address any real template mismatch.

The snapshot includes full local/private JSON and separate balancer overrides.
`execution_sha256` binds both; `effective_config_sha256` binds ordinary JSON.
Raw configuration must not enter SaaS, logs, Git evidence or public descriptors.
A later collector must verify its fresh 32-byte challenge, peer/producer/core
identities, approved executable/build manifest, both hashes and override policy
before projecting a compiler runtime view. No collector is wired in this stage.

No native qualification, physical deny, existing-flow closure, rights, counter
settlement, fleet readiness or VPN profile acceptance follows from source tests.
Agent capability remains false. Builder still retains pending/UNKNOWN apply debt;
the owner-local CAS has not been connected to its transaction. Full PR4 is open.

`NATIVE-PACKET.md` defines the next isolated proof and its outstanding coverage.
The existing stock native receipts cannot qualify this changed producer. Do not
execute the packet before the coordinator's fresh resource/phase ACK and actual
recovery CLOSED/release. No publication, hosted CI, merge or live server work is
authorized by this source checkpoint.
