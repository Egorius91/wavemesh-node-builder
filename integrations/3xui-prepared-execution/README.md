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
CAS is a separate owner-local method. The ROOT943 source follow-up connects
it to exactly one existing admin/session API route; see PRIVATE-OWNER-API.md.
No ordinary Builder activation or additional UI/control endpoint is installed.

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
source/native proof and outstanding full-build/integration gates. There is no
production producer approval or installed consumer/transaction integration.


Corrected consumer native776 passes in an isolated synthetic fixture after the
native epoch adapter correction. Exact current reader bytes and qualified/unknown
Go-wire goldens are pinned. Private consumer output remains in memory; no
production release approval or public/physical/installed readiness follows from
this component proof. The independently closed native slot is released to ROOT.


Current internal producer source0d0b7617 adds an owner-held selector persistence
barrier. The service builds separate actual/template/process selector copies,
uses exact expected generation/epoch and a conditional existing template row
update, then atomically writes the desired process config inside the same owner
callback as the single native call. Config.Clone preserves RawMessage bytes and
panel-only TUIC metadata. Errors retain partial operation facts and require
reconciliation; nil/no-op alone cannot claim commit. That historical0d source had no handler or Builder
positive settlement dispatch. ROOT943 adds a private API/caller source path
without ordinary auto wiring or installation.The source90 checkpoint passed59 Windows/CGO checks, including the unchanged
inline-TLS refusal. The cold desired/actual compatibility correction passes4
affected checks with explicit unchanged-source reuse; neither run has skips. This is not a real
core/full-service/Linux persistence proof; the old35 full-main binary does not
contain this revision. See the current manifest owner_persistence_barrier scope.

Cold launch may block actual execution while persisted desired selection remains
an Exit. The current service patches each independent preimage rather than
requiring those before-targets to match. Native no-op with divergent metadata
still refuses reconciliation. No owner/TLS/control/DTO guard is weakened.

Owner-process postimage correction returns the raw SHA of the same atomic desired
payload after memory adoption. The private journal now checks that postimage
against the bound trusted owner reply; it never substitutes consumed JSON or an
old startup disk file. PRIVATE-JOURNAL.md records the exact three-candidate and
server-postimage boundary, portable58 scope and explicit native/transport gates.


ROOT943 owner API/caller source is specified in PRIVATE-OWNER-API.md: bounded
strict request and exact owner-result body, same-owner full preimage hash,
cancellation at both ownership boundaries, one native call, and a private
one-send loopback client retaining reply/dispatch facts before local completion.
Focused Windows Go24 PASS/no skips, new client8 PASS/no skips, affected metadata
readback compatibility2 PASS. ROOT955 owns the orphan-fact reader guard and
proved its new Windows2/Linux2 cases; only its exact patch is integrated here.
Prior e1 journal native15+4/Bash and reader7/Agent1 remain scoped historical
proof, not native acceptance of this new API/client. Current full main, actual
service/core/caller, loopback ingress/public TLS and physical flow gates remain
open. Source-manifest.json pins current commits/bytes and each proof's scope.
