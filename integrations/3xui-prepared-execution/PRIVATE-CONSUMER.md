# Private snapshot consumer — source proof; Linux integration NOT VERIFIED

scripts/lib/prepared_execution.py provides collect(), returning the existing
private runtime-view3 and complete executing JSON in memory. It prints nothing,
persists no configuration, sends no mutation and adds no Agent/health/API/daemon.
The existing Auto Route caller must obtain a fresh result; saved files, receipts
or heartbeat state are not a replacement for collect(). This phase does not wire
the function into a running transaction or an installed service.

Production trust defaults closed. No approval file or release pin is installed.
The future owning operator must separately approve a full producer release in
/etc/wavemesh/prepared-producer-approval.json. Its parent must be root-owned and
not group/world writable, the file root-owned regular0600 without a symlink.
The strict fields are schema_version:1, artifact_kind:full-producer, source_commit,
upstream_commit, producer_sha256, core_sha256 and release_manifest_sha256. The
source/manifest hashes require a real reviewed full-build provenance. Labels such
as test-elf or synthetic-fixture are refused. This source package supplies no such
production approval, and a test artifact cannot populate it automatically.

The reader probes the private root0700/root0600 Unix endpoint without requesting
configuration, checks the actual peer process/UID/start instance and hashes its
held executable against the approved producer. Only then does it request a fresh
32-byte challenge response. It independently verifies boot, real/effective/saved/
filesystem root UIDs, child parent/start ticks, the actual sealed stock executable
and inheritedfd4, plus matching sealed configfd0/fd3 and full cold consumed hash.
Open references remain held through verification. A second fresh response must
have the same generation, epoch, identities, full current JSON/hash, execution
hash, override state and selector receipt; process objects/seals are checked again.
A change or unverifiable result fails closed, without a cached-success fallback.

Responses are one newline frame ending at EOF, bounded4MiB with one absolute
3s IO deadline. The producer's existing1s guard is unchanged; expensive executable
hashes happen outside open request connections. Unknown fields, duplicate keys,
nonfinite/fraction/exponent/-0 or over20-character integer tokens, unpaired or
invalid UTF8 and U+2028/U+2029 anywhere (including keys) are refused. This explicit
subset avoids silently changing Go's JSON representation. Actual executionJSON
vectors prove supported Unicode/HTML/control strings and wide integer encodings
match the existing Python digest exactly. No producer wire change was needed.

Both complete current-config and config-plus-override hashes are verified.
Nonempty overrides are deliberately unsupported by this projection. A selector
receipt must end at the current generation/epoch/full hash; the existing compiler
still owns route-specific normalized hash/continuity checks. The reader does not
invent a receipt chain or advance health counters. Fixed errors contain no raw
configuration, credentials, paths or exception payloads.

Source proof: reader20 tests PASS0skip with mocked Unix/proc facts,13 actual Go
canonical vectors, and current producer owner21+17 race cases PASS. These are
source/synthetic checks. Actual Linux consumer/peer/proc/descriptor integration
and a full producer build remain NOT VERIFIED. Native757 custody/mutator proof
is retained only within its tested source scope. A new native run needs the
coordinator's actual resource release, phase ACK and fresh admission.

The actual Builder Entry remains unsupported by managed qualification: Nginx
terminates public TLS, Xray receives loopback XHTTP security:none and stock strips
externalProxy metadata. No public TLS inference or live inline-TLS conversion is
made. The known wm-api-rule label is accepted only in the exact unconditional
reserved API rule; other controls remain refused. This fixes an inert shape, not
the Entry TLS gap. Optional generated SOCKS inbounds remain unsupported too.

Contract candidate for ROOT, not an implemented health/public profile: native
execution schema/generation/epoch and full digest identifiers may describe a
fresh private readback. Public TLS, physical deny, path health and profile output
stay not_verified; prepared capability remains false. ROOT still owns Node/tenant/
placement/generation/revision/material/attempt/quota composition and Agent/SaaS/PR3
integration. Raw JSON, relay credentials, UUIDs, Exit addresses, process data or
unreviewed public profiles do not enter health. CAS/persistence settlement,
watchdog, IPv4/IPv6/DNS/existing-flow and management-loss proof are separate gaps.


The first real Linux consumer run failed closed because the adapter expected the
compiler's mutation_epoch field on the native wire. The native DTO uses epoch;
that field now maps explicitly to runtime-view3.mutation_epoch. No producer wire
was changed and the compiler alias is refused on input. Unit snapshots now come
from the actual Go ExecutionSnapshot serializer for both qualified and unqualified frames,
with a dedicated alias regression. Unqualified frames are refused before requiring
private config/hash payloads, which Go correctly omits.
The failed attempt and independent cleanup are retained; current native rerun is
pending. This supersedes the earlier hand-constructed unit header assumption.
