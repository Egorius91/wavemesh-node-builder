# Private 3x-ui execution attestation checkpoint

This is an unpublished PR4 source checkpoint with scoped native custody proof.
Full producer, flow and service acceptance remain open. Apply the
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
It executes the sealed image from inherited fd4. The SAME sealed config file
is assigned as stdin and retained as fd3, using stock -format json -c stdin:.
Cold start checks the actual full executable SHA; continuous checks require the
same sealed executable inode and process instance. Its owned expected descriptor
stays open for that generation and is closed by that generation's actual exit.
An actual native API response is required before qualification. Control is a
filesystem Unix socket at `/run/wavemesh-xray/core.sock`; the read-only snapshot
is `/run/wavemesh-xray/snapshot.sock` in the same producer process. The parent is
root-owned 0700, both sockets 0600, and Unix peer credentials are checked. Managed
control has no TCP fallback. Existing normal callers use explicit API endpoints.

Known unsafe native-control redirection is refused under the mutation owner lock
before calling the core. Override targets must be declared, unambiguous outbounds
outside the reserved control path. Refusal preserves the previous safe hash/epoch;
it does not assert that an unsafe requested effect was applied. Qualified success
also validates every remaining override reference after related native effects.
Removing a referenced target makes the view UNKNOWN; clear/SetConfig cannot heal it.
Supported target/clear and the unmanaged path retain their existing semantics.

All eight native mutators invalidate the view before effects under one owner
mutex. Complete supported success can advance the attested view; errors, lost
responses, unsupported effects, process changes, persistence failure and service
hot/partial paths stay UNKNOWN. `SetConfig` cannot qualify a shadow. Service hot
apply is deliberately conservative and needs a new cold generation; selector-only
CAS is a separate owner-local method with no public mutating endpoint.

Private execution snapshots and path observations use version 3. Every new sample
must bind equal before/after/independently-current generation and epoch. Aв†’Bв†’A
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

The focused native packet passes on the pinned stock core and proves custody,
mutator accounting and override boundaries. It does not prove physical deny,
existing-flow closure, rights, counter settlement, full producer build, real
template compatibility, fleet readiness or VPN profile acceptance.
Agent capability remains false. Builder still retains pending/UNKNOWN apply debt;
the owner-local CAS has not been connected to its transaction. Full PR4 is open.

`NATIVE-PACKET.md` records exact source/artifact/receipt pins, original failures,
current proof applicability, independent CLOSED/release and outstanding coverage.
No native reservation remains; another phase requires coordinated fresh admission. No publication, hosted CI, merge or live server work is
authorized by this source checkpoint.


Current source follow-up adds only the exact inert wm-api-rule label and a private
read-only consumer. security:none remains unsupported; it is not converted to TLS.
PRIVATE-CONSUMER.md specifies trust, fresh readback, canonical hash bounds, the
source proof and outstanding actual Linux reader/full-build gates. There is no
production producer approval or installed consumer/transaction integration.
