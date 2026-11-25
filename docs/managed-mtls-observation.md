# Managed mTLS observation

This source work extends the existing sanitized acceptance collector and Agent
service with a finite observation journal and a separate qualifier. Source and
portable checks are separate from actual POSIX, TLS, PostgreSQL, installation,
supervised restart and admitted runtime evidence. No installed 24-hour interval,
Node admission or service/fleet acceptance is established by this document.

## Local mTLS-only snapshot

The optional `mtls-only` phase uses the existing collector's fixed bounded service,
Agent-check, journal and certificate readers. It requires primary `auth_mode=mtls`,
the existing `mtls_mode=shadow` certificate lifecycle and `SHADOW_ACTIVE` identity.
Bearer, bootstrap authentication, absent primary-auth metadata and unavailable
identity are blocked. TLS package/layout, pending acknowledgement, certificate
validity and existing service/hardening checks remain required.

The local result includes `observation_scope=LOCAL_READINESS_SNAPSHOT` and
`central_heartbeat_observed=false`. It returns only the permitted local metadata.
An installed copy can be invoked with its ordinary read-only acceptance CLI:

```bash
sudo /usr/local/lib/wavemesh-agent/acceptance.py mtls-only
```

Read the resulting `result` and `blocking_codes`. A local PASS establishes the
snapshot's readiness checks. Actual central heartbeat receipt, version/config
bindings, continuity and independent fresh readback belong to the managed run.
The earlier baseline/disabled/shadow/rollback phases keep their response shape
and behavior; the shadow runbook retains its original bearer/shadow scope.

The collector does not invoke key generation, issuance, activation, ACK,
registration, service restart, gate changes or recovery to repair a failed
snapshot. Preserve the blocked result and inspect its cause within the current
functional authority.

## Existing service and request ownership

Observation defaults to disabled. The existing Agent `run` lifecycle starts the
writer only when `WAVEMESH_AGENT_MANAGED_OBSERVATION_MANIFEST` names a separately
admitted manifest. Constructing an Agent, installing its files and running
`node_agent.py check` do not start an observation writer. The installer adds no
manifest setting and does not start/restart the service.

The existing primary mTLS request owns authentication and the response. An
optional callback is considered only for its exact successful POST heartbeat,
HTTP204 and empty body. One canonical bounded header must correlate with the
actual Node/tenant, request timestamp and selected public certificate fingerprint.
Bearer, bootstrap, other routes/methods, duplicate headers, malformed metadata,
foreign correlation and absent metadata cannot produce a positive observation.
The callback never receives arbitrary headers, key material or an unfiltered
response. It buffers the validated receipt; journal work happens after the
existing runtime completes its ordinary request/state transition.

Probe, callback or disk failure cannot turn a committed heartbeat into a retry.
Lost replies remain failed observations. No second heartbeat/transport, PKI
ledger, issuer operation, observation REST route, DB table or service is added.

## Finite run and retained history

The fixed namespace is
`/var/lib/wavemesh-agent/managed-observation/<32-lowercase-hex-run-id>/`.
The directory and run files must be root-owned/private, with no symlink traversal
or linked/replaced regular files. `manifest.json` fixes the exact schema defined
by `validate_manifest` in `agent/managed_observation.py`:

| Area | Required binding |
| --- | --- |
| Identity | Run ID, synthetic/installed scope, Node correlation digest |
| Authority | Actual human-window receipt digest and absolute stop |
| Time | Not-before, collection stop, required duration, final-readback reserve |
| Cadence | Exact Agent heartbeat interval, maximum gap, clock drift bound |
| Storage | Maximum samples and total bytes; space reserved for terminal evidence |
| Sources | Installed Agent/dependency bundle and selected effective config digests |
| Producers | Separately admitted API and ingress artifact/instance bindings |
| Readback | Exact central reader artifact, public effective gate digest and deployment-preflight digest |
| Clock | Admitted independent clock evidence and boot binding |

`admission.json` must bind the frozen manifest, actual human receipt, exact Node,
scope, stop, deployment preflight and clock boot/evidence. This is an operator
receipt created only within actual authorization after source/preflight review.
An example, checkpoint, proposed JSON, digest or an `approved` field written by a
caller grants no runtime authority. This source change creates no such admission.
Installed-node scope refuses a requested duration below 86400 seconds. A short
synthetic interval is never labeled24-hour acceptance. The actual remaining
window must fit collection plus request/cadence and final-readback reserves.

The writer holds one POSIX exclusive lock, verifies the lock/directory inodes,
and uses exclusive creation with file/directory fsync. Every ordinary heartbeat
observation gets an immutable attempt **before** probes/request and an immutable
sequence-numbered result afterward. A lost reply, crash or uncertain write cannot
erase the original attempt. An unresolved/partial attempt, foreign file, hole,
replacement, invalid schema or storage exhaustion blocks qualification. History
is never truncated, overwritten, silently repaired or deleted to continue.

Wall/monotonic time, boot, service process identity and actual NTP synchronization
are observed. Public source/config/CA/service-unit bindings are checked; private
keys, raw environment, Node config and credentials are not journal payloads.
Current service identity comes from fixed bounded systemd/proc metadata. An
unmodeled service drop-in or daemon-reload requirement prevents positive evidence.

Failed observations and missed cadence open retained segments. A service/boot
change cannot stitch two short intervals into a long one. Rotation must have
acknowledged overlapping validity and independent exact replacement lineage.
Clock/source failures retain a blocked run. Restart reads and verifies history;
it never fills missing samples or reopens a stopped/blocked run.

The collection stop ends the qualifying interval. The next ordinary heartbeat
can supply a final nonqualifying readback anchor within the separately reserved
human window; it does not extend the measured interval. Successful finite stop
creates `STOPPED.json`. Interrupted/blocked outcomes remain separate. No writes
are permitted after the human stop; missing terminal durability remains UNKNOWN.
The Agent's ordinary duties keep their existing lifecycle.

Server receipt metadata must come from the authenticated credential context and
actual committed heartbeat time. Existing business observations and expiry-pilot
operations keep their own purposes. The new observer must not create another
transport, certificate state machine or issuer operation to obtain evidence.

## Independent final readback

`managed_observation.py` provides two commands for the owning admitted environment:

```bash
/usr/bin/python3 -B /usr/local/lib/wavemesh-agent/managed_observation.py \
  prepare-central-query --manifest '/var/lib/wavemesh-agent/managed-observation/<run-id>/manifest.json'

/usr/bin/python3 -B /usr/local/lib/wavemesh-agent/managed_observation.py \
  qualify --manifest '/var/lib/wavemesh-agent/managed-observation/<run-id>/manifest.json' \
  --central-readback '/var/lib/wavemesh-agent/managed-observation/<run-id>/central-readback-<tip-prefix>.json'
```

Replace placeholders only with reviewed run paths. The first command verifies a
stopped immutable history and prepares a bounded private canonical query file.
It does not print raw Node/tenant/credential identities. Use the owner-controlled
channel to feed that file to the installed API reader:

```bash
node dist/scripts/managed-mtls-observation-readback.js --manifest -
```

The API reader accepts bounded canonical stdin and opens a bounded repeatable-read
transaction with `SET TRANSACTION READ ONLY`. It verifies the actual PostgreSQL
read-only state and clock, the exact Node, current heartbeat, requested credential
lineage, acknowledgement/validity and unresolved issuance/operation state.
Only selected correlation/status/time metadata is returned. No application module,
worker, issuer, pilot mutation, schema migration or new HTTP route is started.
No arguments are inert. Credentials/secret-bearing columns are not exported.

The configured API producer and public gate bindings are declarations, not runtime
attestation. They require the admitted independent source/deployment/executor
preflight. A generic caller-provided PASS JSON or a hash chain is insufficient.
The returned artifact must use the fixed private readback filename for the exact
journal tip. Its actual reader artifact, query/execution custody and producer
applicability belong to the independent acceptance evidence.

The qualifier reads history under a separate shared lock, requires finite stop
and no blocked/unresolved attempts, then obtains a new concrete local readback.
It reads existing status and the selected public leaf without instantiating
another mTLS state owner or reading a private key. It validates current service,
source/config/clock/Node/certificate bindings, exact central heartbeat equality
and credential lineage. The central read must complete before a later ordinary
heartbeat advances that equality; otherwise qualification is BLOCKED. Do not
resend a heartbeat or relax the correlation gate to obtain PASS.

Only one actual continuous segment can meet the duration. Synthetic scope returns
`PASS_SYNTHETIC_MECHANISM_ONLY` with `measured_24h=false`. Installed24-hour contract
evidence additionally needs its real admitted execution/source provenance. VPN,
fleet/master and full beta acceptance remain false in this report.

## Validation scope

Existing14 portable snapshot/two hardening proofs are retained for their matching
bytes. New focused source tests cover parser/cross-language encoding, the existing
transport callback, Agent lifecycle isolation, actual-duration/gap/rotation
decisions and strict independent readback contracts. PostgreSQL reader unit tests
use explicit synthetic query fixtures; they are not actual DB acceptance.
Actual POSIX lock/link/fsync/crash/restart, loopback mTLS, PostgreSQL read-only and
installer/rollback evidence require the reviewed Linux packet. None is inferred
from Windows mocks or source parsing. No live Node/deploy/24-hour run is included
in source preparation.

Rollback keeps existing collector phases and every PKI/operation record. The
existing installer backs up both new helpers and their previous absence; rollback
restores/removes code as recorded. It never removes or rewinds observation history.
Disabling the explicit manifest setting stops new sampling on the next admitted
service lifecycle change; this is an owner action within its runtime authority.
