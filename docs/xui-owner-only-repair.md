# Exact 3X-UI owner-only repair preparation

**SOURCE PREPARATION / NOT_READY.** The production entrypoint returns77 before
reading an input, probing a host, creating a ledger, locking, stopping a service
or changing ownership. `--execute`, `--quiescent` and a supplied receipt cannot
enable it. No production admission adapter is implemented. Source preparation
and mock tests are not native acceptance or repair authorization.

## Scope and trusted boundary

`operations/xui_owner_repair.py` fixes three aliases and seventeen paths per
target: home/bin directories; x-ui, x-ui.sh, three release service examples;
the six geo data files, README, LICENSE, mtg-linux-amd64 and xray-linux-amd64.
Only owner/group1001:1001 to0:0 is implemented. No chmod, reverse chown, byte
replacement, version change, download/install, enrollment or gate write exists.
Targets run sequentially; another target waits for restoration/acceptance.

Cooperating privileged operators explicitly declare operational ownership,
freeze CLI/deploy/updater/certificate-reload/admin activity, and disclose known
helpers/capability recipients. This is the accepted managed-host trust model,
not a Boolean native proof or a forensic claim about arbitrary malicious root.
Known contradictory writers/recipients and uncertain native results STOP.

Private Agent/CLI selection is unnecessary only while all relevant writers and
capabilities are closed through independent filesystem acceptance. Two empty
cgroups alone do not prove this. No env/argv/private config/credential contents
are read, even internally to return a sanitized Boolean. Existing closed
gates remain closed throughout recovery and cleanup; this code never opens one.

## Required unresolved integration

The central contract audit of2026-10-19 found no complete existing live assertion.
An aggregate read-only DB snapshot cannot fence API in-flight requests, legacy
direct panel calls, local transactions, external writers or credential delivery.
Historical8464 source lacks the newer in-transaction provisioning guard; source
equality must not be transferred to its runtime. The production CLI therefore
stays inert. A future reviewed adapter must supply fresh assertions, not cached
JSON, a user flag, a mock callback or a fabricated receipt:

- `assert_closed()`: exact applicable server/source/config authority, actual
  closed writer/delivery gates, command receive/started/materialize/result and
  pending-transaction reconciliation, continued operational ownership, known
  external/delegated writer closure and reactivation exclusion;
- `assert_target_binding(binding)`: the actual owned target/operation and
  operator namespace/source/manifest authority, without private selector reads;
- `assert_restore_permitted()` and `assert_agent_restore_permitted()`: admitted
  same-version baseline restoration including ordinary lifecycle effects, no
  unadmitted CREATE/SIGN/enrollment; command admission remains closed;
- `assert_panel_restored()` and `assert_restored_and_cleanup_accepted()`:
  independent panel/core, service/job, cleanup and agreed client acceptance;
- `assert_corrective_start_permitted(unit)`: explicit reserve after fresh
  terminal-job reconciliation, not a retry authorization inferred from failure.

These names specify the library seam, not a new service, framework or implemented
authority. `UnboundAdmission` always raises NOT_READY before native probes. No
SaaS writer/gate/DB adapter is imported. The central source package and its
coordinator review remain separately owned inputs to the preparation report.

## Fresh inputs and exact source/schema binding

The manifest has exactly schema1,target,operation and ordered objects. Each
object contains only path,kind,dev,ino,mode,size,mtime_ns,flags,nlink,sha256.
All fifteen regular objects require hashes; both directories use null hash.
Unknown/missing/extra fields, noninteger numerics, extra/duplicate paths/inodes,
hardlinks, special/group-other-write bits and immutable/append flags STOP.
Maximum128MiB per file/512MiB total; seventeen exact entries, no enumeration of
excluded files. Historical51-object metadata JSON is not this schema and cannot
be passed as a current executable manifest. No blind converter/rebaseline exists.

`NativeObjects.capture(target,operation,expected_identities,assert_closed)` is
a future read-only finite admission: expected dev/ino for every exact object
must first bind to the accepted51-object identities; a replaced object STOPs
before content read. Capture hashes only same identified public files, checks
type/owner/no-follow/nlink/mode/xattr names/flags and performs a second finite
pass. It never changes owners. Any changed historical identity needs its proper
review/reconciliation owner; do not silently adopt it. Fresh manifest canonical
SHA256 must be reviewed/pinned before apply under the final functional scope.

The future launcher must use an already trusted OS/Python/bootstrap and a
known clean environment with isolated Python (`-I`), no private environment
inspection. Deploy only reviewed bytes at the fixed OPERATOR_PATH; NativeRuntime
rechecks root-safe ancestors, no-follow root-owned single-link source and its
actual delivered SHA256. Git blob bytes and delivered working-file bytes are
recorded separately by the preparation report when checkout conversion differs.

NativeRuntime requires pinned systemctl/busctl hashes and exact allowed public
unit activation/property pins. It opens/hashes their root-safe executable inode
and executes the descriptor with only a fixed minimal environment, cwd `/`, no
stdin and no inherited private descriptors. Public policy pins are expectations
checked against live properties, not a quiescence receipt. Unknown/unsupported
properties, changed activation paths or source/namespace drift STOP.

## Future admitted sequence and executable recovery interfaces

No invocation in this section is authorized by this document. A future reviewed
adapter/launcher is needed; the current CLI cannot execute these library APIs.

1. Bind target, source, canonical manifest and human operational ownership;
   establish live central/local admission and restoration/recovery budgets.
   `NativeRuntime(admission,command_pins,activation_pins,operator_sha256)` fails
   before native reads if admission is unbound. `open_native_journal(document,
   runtime)` creates the protected ledger only after live/source binding.
2. `run_window(document,runtime,NativeObjects,journal,deadline=...)` records the
   baseline/old process identities under the existing shared lock, requests at
   most one Agent stop then x-ui stop when each was active, records the returned
   manager job and observes that same job. No second stop is automatic.
3. Fresh live checks require known inactive/dead units with Job/MainPID/ControlPID
   zero, supported empty exact cgroup-v2 leaves and old process identities gone;
   fdstore Max/count zero; supported stop/activation policy; no reactivation.
   Known out-of-cgroup delegation/local transactions remain part of the live
   adapter/operational boundary, not inferred from cgroup emptiness.
4. Retain/recheck the existing root-owned single-link0600 lock inode without
   create/truncate/unlink/sidecar. Root-owned sticky1777 `/run/lock` is allowed
   through its specific guard; generic writable ancestors remain refused.
   Descriptor/named HOME/bin and root-prefix bindings, public bytes, metadata,
   xattr names/flags and live quiescence are rechecked per member, including
   skipped root subset. Seal home then bin then exact remaining objects.
5. Each fchown has a durable INTENT before the syscall and APPLIED only after
   readback. Ledger is protected root0600 under a root-safe operation directory,
   bounded, canonical, checksummed/chained, fsynced and rejects torn records,
   replacement/concurrent change or a different operation/source/manifest.
6. An independent freshly opened `NativeObjects` set verifies all seventeen
   root owners and preservation while quiescence/lock remain held. No writer
   is restarted before FILESYSTEM_ACCEPTED. Release shared flock after that
   acceptance; upstream admission/operational ownership remain held.
7. `restore_window(document,runtime,NativeObjects,journal,deadline=...)` performs
   a fresh independent same-object reconciliation whenever object intents exist,
   including after an old full acceptance. Drift blocks service starts. Restore
   x-ui first, accept panel/core, then admit Agent's baseline lifecycle restore.
   A baseline-inactive unit is never started and must remain fully closed.
   Final result explicitly says command resume was not performed.

On an uncertain stop/start: preserve SERVICE_INTENT/job/operation handles and
call `reconcile_window(runtime,journal)` for fresh exact-unit observations.
An active job means observe the existing job; no request replay. An uncertain
fchown leaves durable INTENT and root subset intact: `reconcile_objects(document,
fresh_objects,journal,runtime.assert_quiescent)` establishes actual same-object
state; then `resume_owner_interval(document,runtime,NativeObjects,journal)` may
continue only under renewed admission/quiescence and the same existing lock.
It never reissues a stop. Or use controlled `restore_window` to recover the
partial safe subset; its fresh readback is enforced, not a caller Boolean.

A failed baseline start is not automatically retried. A separately admitted
single corrective reserve uses `restore_window(...,reserve=True)` only after
reconciliation newer than the last request and currently terminal inactive/dead
state with zero control/main/job and empty cgroup. Each unit has at most one
reserve; no corrective stop. Unknown outcome cannot be renamed transport repair.

The cgroup reader supports only the two exact unit leaves on actual cgroup-v2
filesystems, verified with fstatfs using the reviewed Linux x86_64 LP64 ABI.
Unsupported ABI/filesystem, missing mandatory root/system.slice, permissions,
read errors, symlinks, changed named/FD/ancestor bindings or malformed controls
STOP. Only an exact leaf's initial open-ENOENT plus a second anchored negative
lookup under unchanged verified ancestors can mean absent; disappearance after
a successful open cannot. No Path.exists result or skipped subtree is authority.

Existing leaves require zero nr_descendants/nr_dying_descendants in both bounded
cgroup.stat snapshots; all subtrees, including dying descendants, are explicitly
unsupported. Kernel-recursive cgroup.events populated and direct cgroup.procs
must agree and remain stable across the bracket. A stable populated1 with direct
PIDs is usable for baseline capture; populated1 with an empty direct list STOPs.
Empty requires populated0 plus empty procs and stable bindings/controls. These
are finite observations within continuously held admission/external-writer
exclusion. They are not atomic or forever-empty guarantees; timestamps are not
general generation counters and cannot exclude every unobserved transient
create-delete between reads. Native mechanics remain NOT_PROVEN until admitted
native verification. Primary semantics: Linux admin-guide/cgroup-v2.html and
include/uapi/linux/magic.h (CGROUP2_SUPER_MAGIC).

No ownership reversal, mode compensation, reinstallation or speculative command
replay occurs. A changed object or partial ledger needs reconciliation; preserve
proven0:0 objects. Full filesystem acceptance and restoration are separate.
Service health does not authorize unknown alternate executor dispatch. Existing
command-resume contract and final functional grant remain prerequisites.

## Effects and tests

Future target budget:17 successful owner transitions; at most one stop/start
per initially active known unit plus one separately admitted corrective start
per unit; three targets sequentially. Ledger/directory creation and shared-lock
acquisition need the functional grant. No runtime gate/drop-in or manager reload
is implemented; if required, prepare its exact previously reviewed paths and
cleanup effects separately within that final functional window. Baseline closed
gates must never be opened to restore service. Admission supplies measured
execution/recovery reserve; no financial15s/240s or old15-minute proposal is used.

Portable focused proof (no native calls):

`python tests/unit/test_xui_owner_repair.py`

Focused virtual-only cgroup regression proof (no host /sys/proc/fstatfs reads):

`python tests/unit/test_xui_owner_cgroup_reader.py`

Prepared native fixture command, **NOT_RUN** in this stage, requires a separately
admitted Linux UID0 fixture window:

`python3 -I tests/native/test_xui_owner_repair_native.py --require-native`

The fixture defaults77, uses only a fresh synthetic temp tree, and never calls
systemctl/busctl/flock/Node/network or live repair. It prepares real fchown,
same-byte/mode/inode/exclusion preservation, HOME rename rejection,
symlink/hardlink refusal and protected-ledger torn-record/reopen checks. Passing
it would prove only POSIX fixture behavior, not a Node's admission/runtime state.
Full CI, native fixtures, staging quiescence/repair, deployment and command
resume remain NOT_RUN. No new workflow or dependency is added.
