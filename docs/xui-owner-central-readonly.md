# Central snapshot for owner-only repair

`operations/xui_owner_central_readonly.py` implements the fixed aggregate from
the accepted `node-central-readonly-admission-contract-20261019/CONTRACT.md`.
It is a necessary observation, not `assert_closed_and_drained`, a lease, writer
exclusion, Node-local drain or permission to change owners/services. The existing
`xui_owner_repair.py` production `UnboundAdmission` and entrypoint remain inert77.

Without `--collect-readonly` the collector returns77 before opening an input,
schema asset, binary or Docker connection. Explicit collection returns0 only for
`SNAPSHOT_CLEAR_NOT_ADMISSION`; debt/invalid/unknown observations return78. A clear
record always retains `liveAdmission=false`, `runtimeSource=NOT_OBSERVED`,
`configPolicyAuthority=NOT_OBSERVED`, `operationalExclusion=NOT_OBSERVED` and
`nodeTransactions=NOT_OBSERVED`. Exit0 is not an admission capability.

## Concrete channel and output

The collector uses the already accepted staging channel: local Docker exec into
`wavevpn-stage-postgres-1`, fixed `sh -c` and `psql -X -q -t -A -v ON_ERROR_STOP=1`
using existing container-local POSTGRES_USER/POSTGRES_DB and a fixed local
`/var/run/postgresql` socket/5432, so inherited PGHOST cannot select another host.
Actual socket availability is a native prerequisite; failure stops. It never reads or
exports credentials, Config.Env, server argv, enrollment policies, IDs, payloads,
command rows or query text. No private controller module/ABI or application
bootstrap is imported. No package/dependency, endpoint, flag, fence or database
writer is introduced. Docker uses only the local Unix socket, a minimal client
environment and the fixed two container names.

A prepared exact Docker binary hash is checked before executing its open file
descriptor. This binds the existing native executable used for observation and
avoids a pathname replacement between checking and executing it. It does not
install a binary, create credentials, expand privileges or establish arbitrary
hostile-root protection. Runtime requires the already admitted native Linux/root
operator context; invoking this program does not grant that context.

Before and after the single SQL session, narrow formatted Docker inspect checks
API and PostgreSQL container Id/Image/Pid/StartedAt/Running/RestartCount. Only
equality/status leaves the process. Docker labels are never source authority.
These checks bind the observed containers during this call; they do not prove
the actual API module/config-loader/server-child process identity, launch recipe,
topology, effective policy, excluded dispatchers or operational ownership.

One RepeatableRead READ ONLY transaction checks schema applicability, then runs
the fixed prepared aggregate with the three target parameters. Limits are5s per
statement,2s lock wait,7s idle transaction timeout and20s across the native reads
including binary hashing; bounded stdout16KiB, any stderr/nonzero/timeout invalidates
the observation. No automatic retry. The Docker client is killed/closed on pipe
failure; the server statement/idle limits bound a surviving psql session. There
is no claim that killing the client proves immediate backend termination.

The schema asset binds source
`c64f32b13df1126aeae8bf25c6f413e84182d8ce`:59 schema/migration **files**, including
`migration_lock.toml`;57 migration directories/SQL files. The exact migrations
and checksums, successful migration-row count,31 SQL-used columns' type/nullability/
typmod and6 referenced enums must match inside the snapshot. A mismatch stops
before the versioned aggregate. Missing/unreadable migration metadata or other
SQL/transport failure also fails closed, with no conditional omission. This is
applicability checking for this statement, not general DB qualification or proof
that triggers/writer guards are operationally installed/held.

These schema/migration bytes equal historical
`8464d0b3704bee3934be8a7b59a39e76963f784c`. The collector makes no deployed-source
claim for either reference. In particular8464 lacks c64's paid-provisioning
in-transaction three-flag check; schema equality does not transfer that behavior.

## Prepared private binding

Trusted native preparation remains a separate runtime prerequisite. The reviewed
public object manifest binds the three aliases and exact public objects, not the
private central tenant/Node/externalId mapping. This source stage does not invent
or automatically prepare that mapping, ask the owner for IDs, or read a private
runtime artifact. The already trusted native preparation must supply a root:root
0600, single-link regular file as stdin and separately admit/pin its SHA256.
The pin proves which input was used; it does not prove the mapping is true or
authorize collection/repair. No example file containing real IDs is stored here.

Binding1 has exactly these fields:

- `schema`: integer1; `contractSha256`: frozen CONTRACT hash; `targetManifestSha256`:
  frozen public EXACT-OBJECT-CANDIDATE hash; `operation`:32 lowercase hex matching
  the existing repair operation selected for this invocation.
- `targets`: exact map of `wm-stage-entry`, `wm-exit-2`, `wm-exit-de-frankfurt-2`.
  Each contains only opaque `tenantId`, `nodeId`, `externalId` strings. Node IDs
  and external IDs must be unique across the three bindings. These are data,
  never authority Booleans or environment/host/SQL selectors.
- `api` and `postgres`: each exactly `containerId` (64 hex) and `imageId`
  (`sha256:` plus64 hex), selected by trusted native preparation for the existing
  fixed staging containers.
- `dockerSha256`: admitted public existing `/usr/bin/docker` binary hash.

After separate runtime authorization and preparation only, the invocation shape
is `python3 -B operations/xui_owner_central_readonly.py --collect-readonly
--target <one-allowed-alias> --operation <existing-operation> --binding-sha256
<admitted-input-hash> < <prepared-root-private-binding>`. The collector accepts
no path, host, SQL, arbitrary environment name, gate value or `closed=true`.
IDs are encoded to hex within the native process and passed on stdin with psql's
standard `:'variable'` substitution and PostgreSQL UTF-8 decode. They never enter
Docker/psql argv or the exported record. All output is a strict bounded aggregate.

## Remaining runtime boundaries

Before this snapshot can be one component of future LIVE admission, the existing
owner must independently establish actual source/schema applicability, exact
compiled modules/config-loader/dependencies and unchanged server launch/process
identity for every relevant replica/dispatcher; the accepted conditional config
equivalence premises and unique literal allowlisted flags; continuously held
target-affecting writer/legacy panel/manual/outbox/lifecycle exclusion; and
independent Node worker/cgroup/transaction-root/pending-transaction and retained
capability reconciliation. No Env/policy value is projected by this collector:
it cannot legally infer those premises from a new docker-exec process or owner
Boolean, so reports that precise authority boundary as NOT_OBSERVED.

Fresh nonzero debt requires its existing reconciliation owner, not payload
inspection, cancellation, replay or a fence write. Held-binding-fence count is
informational. A clear aggregate cannot prevent a later commit or dispatch and
must never be cached as an admission lease. Repair wiring requires separately
reviewed complete live authority. See the report's minimal native proof plan;
portable mocks do not verify PostgreSQL syntax/isolation or Docker execution.
