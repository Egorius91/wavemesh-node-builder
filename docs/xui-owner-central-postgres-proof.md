# Disposable native SQL proof

`tests/native/test_xui_owner_central_postgres.py` prepares the finite native proof
for collector `40100ebda67fe4b3778b3015fe985203016e350d`. Default returns77 with
effects0. Native execution is future CI work, not part of local preparation.

The existing Builder `tests.yml` job gains one4-minute native proof step and one
independent `always()` one-minute finalizer. The job budget becomes15 minutes,
retaining the previous10-minute suite plus these5 minutes. It does not repeat the
accepted four owner/filesystem POSIX cases or add a new broad workflow.

The native proof owns one uniquely named/labeled disposable PostgreSQL container.
It reuses `postgres:16`, already used by SaaS CI, resolves its immutable image ID
once after pull, checks its declared data-volume path, then creates by that ID.
There is no port, network, host bind, persistent volume, privileged container,
private checkout/token, real mapping or application bootstrap. The filesystem is
read-only except three bounded tmpfs mounts; memory256MiB, CPU1 and PID128 caps.
The role/database and rows are synthetic; local trust auth has no exposed network.
Startup waits for the final PID1 postmaster, not the temporary initdb server.

The fixture creates only9 minimal tables containing the31 SQL-used columns,
6 enums and one synthetic `_prisma_migrations` metadata table with57 source-pinned
name/checksum rows. It **does not apply57 migrations**, install their constraints/
triggers, qualify staging schema or establish runtime applicability. Neither the
collector query, its schema asset, parser nor production repair wiring changes.

The real collector `build_sql` and `collect`/strict parser execute against actual
PostgreSQL through a fixture-local fixed Docker exec/psql call. Container inspect
is explicitly fixture-stubbed. This SQL proof does not qualify production
`NativeDocker`; no fixed staging container names are created or inspected.

The finite planned46 collector snapshots are:

- One positive real READ ONLY/RepeatableRead result with quote/backslash/UTF-8
  synthetic mapping, plus wrong tenant and wrong external ID:3 cases.
-35 data cases: every debt predicate; nonterminal commands; attempted terminal
  failures; successful attempted command; cross-tenant anomaly; running/leased
  attempt and completedAt NULL; all unsettled access alternatives; active issuance;
  pending/expired/acknowledged/absent delivery; tenant/global unscoped work; held
  and released informational fence. Expected aggregates are explicit per case.
-6 schema refusals: missing used column, missing used table, changed enum,
  wrong migration checksum, missing migration row and unfinished migration.
- One real lock timeout: a single finite psql holder takes ACCESS EXCLUSIVE on
  the synthetic Node table for4 seconds. The collector must fail once with actual
  SQLSTATE55P03 after its2-second lock wait; no retry.
- One final positive after reset:1 case.

Before/after every normal snapshot and refusal, fixture-owned fingerprints verify
unchanged synthetic table contents. The lock case verifies the same after its
holder ends. Fresh `pg_stat_activity` counts (no query text) must show zero other
client backends, including no idle transaction. Successful collector calls attest
their actual transaction settings. A separate concurrent-writer synchronization
test is deliberately not introduced; no external exclusion follows from isolation.

The setup/reset budget is finite: one image pull, at most one container create and
one start, at most30 startup observations;8 minimal schema rebuild batches,
35 data TRUNCATE batches,43 three-Node seed batches,35 debt seed batches and
6 schema-negative mutation statements. Collector SQL has46 attempts maximum and
does no DML. There is at most one lock-holder client/process. All writes are to
the disposable synthetic fixture. No actual Node/service/payment/issuer effects.

The proof budget is180 seconds, command output capped16KiB, the pull at most60s
and other Docker/psql calls at most20s with the remaining deadline. Lock-holder
cleanup can use at most15 additional seconds; owned-container finalization has
its own30-second budget. A root:root0600 single-link receipt is created exclusively
in RUNNER_TEMP before create intent. It records only synthetic ownership and the
immutable image/container identity; state collisions stop rather than overwrite.

Both primary cleanup and the independent finalizer freshly compare name, unique
label, exact container ID and image before removal. Unknown or rebound ownership
stops removal. At most two force-remove requests for the same owned container are
possible across the two finalizers, and the second requires fresh readback that
it still exists with exact ownership. No recreation, volume deletion, image purge
or blind retry occurs. Successful removal requires independent zero-container
readback; the private receipt remains as the sanitized cleanup evidence. A failed
or uncertain primary outcome remains failure even if cleanup later succeeds.

Local preparation tests are portable mocks/source checks only. Actual SQL syntax,
catalog behavior, lock timeout, container startup and cleanup are NOT_RUN until
main publishes the exact reviewed source and accepts its mandatory CI evidence.
Even a successful future synthetic proof leaves actual migration application,
production Docker transport, staging source/config-policy identity, writer/local
drain and LIVE repair admission unproven.
