# Private intent preparation (unwired)

`scripts/lib/prepared_intent.py:prepare_once(transaction, route_id, files, observations)`
freezes one new schema1 reconcile intent. It has no CLI, transport, installed
caller, capability publication or automatic retry. `auto.sh`, Agent/SaaS and
Producer are unchanged. A prepared intent alone is not an engine transition.

The existing root FD9 owner and one new `auto-prepared-reconcile` transaction
are required. Other unresolved transactions, old pending/request/reply/completed
markers, dangling markers and partial preparation all refuse. Old generic
UNKNOWN debt is never converted or dispatched again.

The writer reads private config/runtime/process preimages and the exact stored
SQL template under a SELECT-only connection. The reader is closed before the
private evaluator collects execution. One internally collected `(view,effective)`
feeds both compilation and intent construction; callers cannot supply trusted
identity/effective configuration. The public evaluator keeps its existing
candidate-only return shape, including capability/applied/settled false.

The intent separates canonical full effective/normalized hashes from exact raw
SQL, config/runtime/process and candidate hashes. Only an approved selector
change is eligible; a no-op or unrelated config/runtime ownership change refuses.
Preimages are rechecked before writes and before the pending marker. Candidates
and preimages use existing durable journal functions; pending is written last.
Every barrier must return successfully. Failure retains original facts and raises
the fixed `PREPARED_INTENT_NOT_FROZEN`; callers must not invoke the owner after it.

After a successful return, a future admitted owning adapter may invoke the
existing `prepared_owner.apply_once` once, with an admitted existing credential
file. That helper rechecks its frozen context and records dispatch before sending.
No wrapper that sends is installed here. Partial/unknown native outcomes retain
debt for read-only reconciliation; reverting this source does not compensate
already-dispatched state.

Portable contracts are `tests.unit.test_prepared_intent` and the changed private
evaluation tests. They mock only collector/FD9 in those explicit fixtures and do
not establish Linux custody or durability. `tests.native.test_prepared_intent_native`
uses actual root FD9/flock and native journal barriers in a fresh isolated guest,
while its collector remains synthetic. It must be selected separately; no skip
is accepted as a native PASS.

`tests/native/prepared_full_composition.py` prepares a separate full integration
proof using the actual accepted ELF, pinned stock Core and normal synthetic DB
and GetXrayConfig. It uses unchanged real approval/collector/peer/API/owner/
completion checks, with no monkeypatch. Its explicit synthetic health samples
exercise selector composition only. It must run inside a separately admitted
fresh guest/private mount and network namespaces with only loopback. Global
underlay addresses and policy-owned observatory probes cannot leave that network
namespace. Optional subscription/bot/LDAP/SMTP/WARP/remote-routing integrations
are disabled, and nodes/clients/outbound-subscriptions must be empty.

The Producer's 5s ReadHeaderTimeout and all native/control/auth timeouts remain
unchanged. Actual full-ELF collection after TCP connect may expose a timing gap;
failure is retained without replay or relaxing guards. No installed approval,
positive capability, public profile or physical IPv4/IPv6/DNS/new/existing-flow/
stale/management-loss/hysteresis/no-RU acceptance is claimed by these helpers.
