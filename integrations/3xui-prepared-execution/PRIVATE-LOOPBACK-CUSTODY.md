# Exact generated loopback ingress custody (ROOT948)

Private schema3 qualification means custody of the exact consumed/effective
configuration. It does not independently prove a public TLS handshake. This
source adds one bounded local input class; public channel v1, TLS descriptor,
profile refusal, Node capability and public admission remain unchanged.

## Actual serialization provenance

`tests/unit/loopback_fixture.py` uses the existing `prepared_channels.stage`
and `runtime_state.inbound_payload` functions. It retains the full payload,
including panel-only externalProxy, sniffing, allocation and client metadata.
One case is the actual unpublished prepared route with no clients; a second
synthetic legacy managed rendering carries one enabled and one disabled client
to exercise dynamic normalization. It is not a published prepared profile.
The same generated JSON is copied byte-for-byte into Producer testdata.

The service test uses existing temporary-DB/ClientService.SyncInbound seams and
the real GetXrayConfig/GenXrayInboundConfig path. A controlled supported template
provides the reserved API and inert synthetic router/outbounds; it is not a claim
that arbitrary stock templates qualify. The ENTIRE resulting Config is retained
as `builder-getconfig-loopback.json`, without clipping the inbound. The actual
owner strips externalProxy and normalizes clients to id/email only, filtering
disabled records. Allocation is absent from core output because the existing
model converter omits it. Both serialized cases were refused RED on prior8923.

Only the test binary's explicit `-update-builder-loopback-fixture` flag regenerates
the fixed golden path. No production test bypass or runtime flag is added.
Ordinary tests require byte-identical real GetXrayConfig output. The Builder
test rerenders its input fixture through the real renderer as a separate proof.

## Added class: exact keys and types

- Inbound has exactly listen/port/protocol/settings/streamSettings/tag/sniffing.
  Listen must be literal string127.0.0.1; port is integral JSON1..65535; protocol
  is vless. Nonempty wm-route- tag is ASCII alphanumeric then alphanumeric,
  underscore, dot or hyphen, maximum total128 characters.
- Settings has exactly clients/decryption/fallbacks. Decryption is none;
  fallbacks is an empty array. Clients is an array of exact id/email objects;
  id is a nonzero canonical UUID, email is an ASCII alphanumeric/underscore/dot/
  at/plus/hyphen label of length1..256. Duplicate IDs or emails refuse. Flow,
  reverse, enable, passwords and extra keys are not the normalized class.
- Stream has exactly network/security/xhttpSettings, values xhttp/none and an
  exact path/host/mode object. Mode is stream-one. Path is literal /api/ followed
  by one or more nonempty ASCII alphanumeric/underscore/hyphen segments each
  ending in slash, maximum256. Host is nonempty bounded253 LDH labels, each
  length1..63 with no edge hyphen. URL, query, traversal, control and empty forms
  refuse. These are local XHTTP matching strings, not public TLS acceptance.
- Sniffing is the exact generated enabled=true, destOverride ordered
  http/tls/quic/fakedns, metadataOnly=false, routeOnly=false object.

For this added security:none class, missing/wildcard/public/hostname/IPv6-alias
listeners, invalid port/tag/protocol/transport/mode/decryption/path/host/client
data, TLS/reality/externalProxy/extra transport/extra top-level fields all refuse.
The global external-input/numeric and native-control guards still run. Existing
TLS class behavior stays unchanged; this does not convert another inbound into
the new class. The older incomplete hand-modified loopback negative is unchanged
and still refuses, as are native API escape/mutable dependency negatives.

## Limits

Focused Windows source validation and real synthetic GetXrayConfig generation
do not prove actual Linux sealed launch/core qualification or a loaded service.
Those must be included in the final related native packet. ROOT958 snapshot
cancellation and pre-credential connected-peer custody remain separate required
fixes before the full main build. No API/client/auto activation, public TLS,
physical new/existing IPv4/IPv6/DNS denial, publication or live action follows.
