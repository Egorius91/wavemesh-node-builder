# Exact-source native packet — NOT RUN

The producer source commit, patch and file hashes are pinned in `source-manifest.json`.
The companion Builder commit pins the private v3 compiler. Rebuild the Linux test
binary from these exact bytes; an earlier `execution-linux.test` in a tool cache
is not admitted. The latest cross-compile attempt caught a native-test protobuf
field mismatch; it was corrected to `Reset_`, but no retry was performed before
the coordinator's source freeze boundary. Current Linux linkage is NOT VERIFIED.

Prerequisites: fresh coordinator phase/resource ACK; recovery's actual independently
checked CLOSED/release; Windows reserve at least 2048 MiB; an exclusive own profile,
new source manifest and overlay; no shared folders/host credentials or live data;
exact stock core hash; available Linux toolchain/dependencies in an owned prepared
baseline. Do not mutate an existing sealed base. Absolute stop 2026-11-16 19:15 UTC.
No VM or native reservation belongs to this checkpoint.

Within that newly admitted guest, build and run only the targeted package:

```sh
go test -c -trimpath -o /work/pr4/producer-stage1-native.test ./internal/xray
XRAY_E2E_BINARY=/work/pr4/xray-linux-amd64 \
  /work/pr4/producer-stage1-native.test \
  -test.run '^TestExecutionNativeImmutableInputUDSAllMutatorsAndLostOutcome$' \
  -test.count=1 -test.parallel=1 -test.v
```

The fixture uses `NewProcess` with the actual stock core, synthetic inline TLS,
loopback-only targets, an owned temporary binary copy and the fixed private Unix
paths. It has no panel/DB/Node registration, provider access, real rights or money.
It cannot run when the private socket paths are already occupied. It stops only
its own child; cleanup must independently verify child/ports/socket closure and
the VM/overlay lifecycle before release. Preserve failed receipts and process IDs.

Implemented but NOT RUN checks: immutable consumed fd seals and rejected write;
consumed bytes/hash; real stock native Unix dial; actual producer/core Unix peer
identity; 0700/0600 ownership; no managed TCP listener; nobody-UID direct access
rejection; all eight native mutations; user/override handling; same-process selector
CAS/receipt; stale CAS refusal; target and unrelated sentinel routing decisions;
non-reset native API traffic counters; native failure remaining UNKNOWN despite
SetConfig; stop and a fresh cold generation. These assert no physical flow deny.

Coverage still required after this packet: real end-to-end client IPv4/IPv6/DNS,
prepared Exit target/sentinel traffic, existing-flow closure on all-down/stale/
watchdog, management-loss continuation for issued rights, concurrent service-level
partial hot paths, full Linux producer build and real collector/Builder CAS
integration. Snapshot/config or TestRoute results do not substitute for those
checks. No positive Agent/SaaS capability may be enabled from this packet alone.

Admission or outcomes that cannot be proved stay NOT VERIFIED/UNKNOWN. Do not
patch Xray, substitute TCP, remove pending debt or repeat an ambiguous business
operation to obtain PASS. Return the precise failed guarantee for architecture ACK.
