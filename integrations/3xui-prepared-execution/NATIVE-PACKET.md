# Scoped native custody proof — PASS; full PR4 remains open

The manifest pins current producer `e3c7fc18928bfbe16de4e0ffa163935e166ca0e1`.
Actual Linux exec1245 ran source `253ace5365c804f84746442bc9f15ed75f803a2b`,
ELF SHA256 `db47f84136ec3200458fd24bdc16bdd274b79e0a1da2e1257c8978d592127852`,
with unchanged stock Xray26.10.21 SHA256
`a27e3f95ab6ce270956d0cecd32eda4686a811afc71c64a6a734a3dede14883e`.
Three selected tests PASS, zero fail/skip. The current producer differs only in
one portable lifecycle assertion: ReadAt checks ErrClosed on Windows, whose
File.Stat returns ERROR_INVALID_HANDLE. Product, Linux identity tests and the
actual-core fixture are byte-identical to the native-tested source. Current
focused Windows race proves the corrected portable assertion. The original
Windows failure is retained, not relabelled as a passing grouped run.

Evidence is recorded in `PR4-PHASE757-NATIVE-RESULT.json` and the source manifest.
The native receipt SHA256 is
`54450df8ec839dbb220afe5f6816ec980a90e76357aea14f9b8d9b9c55768e8e`.
Independent guest1284 cleanup found no owned producer/core/helper processes,
execution socket entries or helper files. The VM is CLOSED: original PID15680
absent, QMP62440/QGA62441 have no listeners, qemu inventory empty, serial PowerDown,
qcow2 check zero errors and both sealed profile hashes unchanged. TCP connection
probe timeouts are recorded separately and are not called connection refusal.
Release: `PR4-PHASE757-NATIVE-RELEASE.json`, SHA256
`e43b42aeae31cc3825629b4dd4b989bd81bbd274735602d3cb7ca80d86d1cf75`.
No VM/native reservation is retained by PR4.

The first packet exposed two real compatibility gaps. The synthetic VLESS fixture
needed explicit decryption:none. Stock's regular-file loader resolves symlinks,
so a /proc fd path for a memfd is rejected. The corrected producer passes the
SAME sealed config file as stdin and inherited fd3, using stock's native stdin:
input. No pipe, mutable file or Xray patch is substituted. Actual fd0/fd3 have
the same device/inode, full consumed hash, all immutable seals and EPERM writes.

The 1s snapshot deadline exposed repeated full executable hashing under the owner
lock (actual Go SHA measurements640–991ms for35MB). Cold start still validates
the actual executable's full stock SHA. Every subsequent identity check validates
the actual process's executable against an owned descriptor retained for that
whole generation: same dev/inode plus all write/grow/shrink/seal seals, running
PID and start ticks. Closed/missing expected handles, replacements and incomplete
seals fail closed. Actual exit closes only its captured generation's descriptor.
The 1s deadline and native assertions were not increased or weakened.

The three native tests cover sealed-object identity (same object, same-content
replacement, missing/unsealed/partially sealed/closed objects), generation-local
cleanup, and actual managed stock-core custody. The last covers private Unix
ownership/modes/peer credentials, nobody-UID denial, no managed TCP listener,
all eight mutators, selector CAS and stale conflict, target/sentinel route
choices, non-reset API counters, failed effect remaining UNKNOWN despite
SetConfig, safe cold generation, pre-effect unsafe override refusal without
core/hash/epoch changes, supported target/clear and referenced-target removal
remaining UNKNOWN. It does not assert physical client traffic closure.

The packet was cross-linked with Go1.27.4 for Linux/amd64, CGO disabled, and run
as a synthetic root fixture in an exclusive disposable restricted-offline VM.
It proves this package and exact native artifact, not the full producer's
SQLite/frontend/web build, real templates, installed service or real servers.
The original startup/snapshot failures, all source pins and receipts remain.

Remaining gates: production snapshot collector and approved producer manifest,
Builder CAS/settlement/persistence, watchdog, real end-to-end IPv4/IPv6/DNS,
all-down/stale/existing-flow closure, issued-right continuation during management
loss, PR2-owned Agent/SaaS integration and service/Node/fleet acceptance.
Positive capability remains false. No publication, hosted CI, merge or live-server
work was performed. Any next native phase needs coordinated resource admission;
absolute stop2026-11-16 19:15UTC/22:15MSK remains unchanged.
