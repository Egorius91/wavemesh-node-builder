# Prepared European Exit paths (PR4 source contract)

This additive local compiler supports the existing VLESS / XHTTP / stream-one /
TLS channel on an exact Xray executable. IPv4 and IPv6 are separate underlay
paths. They do not turn one VPS into two failure domains. Existing v1 Exit join
manifests and client credentials are unchanged.

The source capability is `prepared_eu_paths_v1`. Its public descriptor is built
by an allow-list and contains the channel tuple, exact engine version/hash,
address-family set and `all_paths_down=block`. It contains no Exit address,
panel, relay path or interserver credential. A declared capability is not an
installed Node or service acceptance receipt.

## Private plan and local owner

`schemas/prepared-channel-plan-v1.schema.json` describes the private input. Bind
the existing imported UpCloud and Infomaniak (CH) Exit identities and manifest
checksums, distinct operator-verified failure domains, exact channel capabilities
and literal addresses already present in each imported manifest's expected IPs.
Each Exit must retain IPv4. IPv6 is optional additional coverage; absence remains
NOT VERIFIED, not IPv6 acceptance. Metadata alone does not prove geography,
provider independence or usable connectivity. No VPS is ordered or replaced.

The plan must bind the installed `/usr/local/x-ui/bin/xray-linux-amd64` version
and SHA-256. Version reporting, executable bytes and native parsing are separate
checks. Unknown combinations, mismatched manifests, private addresses, duplicate
paths and Xray selector prefix collisions are refused. The existing v1 import
and outbound renderer still own the relay credentials and TLS identity.

After explicit runtime admission, the existing local mutation owner can use:

```sh
wavemesh cascade auto prepared install --id europe --plan /protected/plan.json --runtime-view /protected/current-runtime.json --effective-config /protected/executing.json
wavemesh cascade auto prepared reconcile --id europe --runtime-view /protected/current-runtime.json --effective-config /protected/executing.json --observations /protected/observations-v2.json
```

These are source interface examples, not authorization to run on a VPS. Install
requires an unpublished, materialized Auto Route and starts with its entire
client inbound blocked. Reconcile consumes the owning collector's complete
qualified full-path observations, then selects only one admitted path. That
collector must exercise each prepared VLESS/XHTTP/TLS outbound through the
existing real data-path mechanism and bind executing instance/config before
and after. The backup outbounds already exist before failure. There is no
provider CREATE in this path. Legacy overrides/toggles cannot bypass a prepared
plan. Existing SaaS admission must be integrated before public publication.

## Health, fail-closed and rollback

Explicit health policy supplies failure/recovery sample counts, sample interval,
maximum collection age and cooldown between forwarding selections. These are
operator inputs, not measured availability promises. The existing route-health
transition helper owns the counters. Only distinct, complete and fresh samples
advance them. Gaps reset consecutive history; replay, future times, engine or
configuration mismatch refuse admission. Collection starts before the first
probe, so slow requests cannot extend freshness. A complete all-down sample
blocks immediately, regardless of hysteresis or cooldown. Stale state compiles
to block. A periodic tick/watchdog is still required to enforce that decision
on an installed runtime when the controller stops; no new service is installed
by this source package.

Managed client rules precede user/catch-all rules and have no destination,
protocol, IP-family or port exception. Every managed balancer has a blackhole
fallback. A retained per-inbound guard prevents route removal from exposing the
default direct outbound. Unrelated inbounds, rules and observatory subjects are
preserved. The local Auto health reader labels these observations as
`local-entry-path-only`, independently of financial or service readiness.

Operations reuse the existing lock, snapshot and compile/native parse owner.
The intended mutation → settled executing readback → validation → atomic
config/runtime commit flow still needs the shared settlement integration below.
The saved config, effective template and stored SQLite template must prove a
safe recovery policy before restore. Missing rules for an active saved Entry
or an unsafe fallback refuse rollback. Existing durable `rollback_failed` debt
blocks another mutation until recovery is resolved. An old live Auto Route
without a provable EU-only snapshot is deliberately refused before mutation;
its migration needs a separately reviewed fail-closed transition. Returning to
an unsafe historical template is not an upgrade strategy.

Before a prepared Xray POST, the transaction persists the candidate hash and a
pending-effect guard. HTTP success does not clear it. Missing settlement,
lost/failed replies or interruption leave the guard, stop automatic restore
and retain `rollback_failed` debt. Recovery refuses while that effect remains
UNKNOWN; a readback alone cannot prove that a still-running writer is finished.
Validation failures before dispatch record a no-effect rollback without sending
an unnecessary Xray restore. Unsupported engines therefore stop before POST.

## Integration and qualification remaining

PR2 retains `agent/access_runtime.py`, `agent/node_agent.py`, rights/quota,
membership, command/material/result and SaaS readiness. PR4 route evidence must
compose after the exact current placement/generation/shared entitlement revision,
local command version, plan hash, accepted material/result/attempt and quota
admission. A public descriptor never grants access or clears deny/debt. The
source form is coordinated in `PR4-PREPARED-EU-PATHS-20261116-v1`; authenticated
receipt transport, periodic tick/watchdog and SaaS consumer integration remain
with the existing owners. No second readiness or entitlement store is added.

The native fixture uses pinned Xray 26.10.21 from the previously verified
3X-UI 3.11.0 release in a fresh loopback-only network namespace. It tests full
TLS/XHTTP paths over both underlays and router blocking against a sentinel. It
does not stand in for 3X-UI/nginx/systemd transactions, real UpCloud/Infomaniak,
RF reachability, active-session preservation or provider failure independence.
It explicitly installs an ephemeral verifying CA in the fixture outbounds and
permits only the fixture's loopback TCP receivers in the synthetic Exit's final
rules. The production compiler's public-CA trust policy is unchanged. Native
DNS/UDP and target/sentinel results are recorded separately from service proof. The
PR91 different-UUID/shared-subId limitation remains open; this compiler neither
rotates those identities nor declares that limitation solved.

Current-head hosted gates are required before merge. Live Node/VPN/Entry and
PR8 Happ/INCY/fleet acceptance remain separate. Local source/native evidence
cannot open production or finite-quota admission.

## Process-bound observation v2 — integration in progress

Stock engine evidence shows that panel HTTP success can precede replacement of
the running Xray, even when executable version/hash and desired config match.
Private observations therefore use schema2 with before/after process identity
`{boot_id,pid,start_ticks}` and the qualified effective-config digest. Public
channel/join descriptors remain version1. A schema1 private observation cannot
establish prepared-route admission.

The existing Agent collector must supply an independently CURRENT, SETTLED
runtime view. Missing, ambiguous or pending transitions have no qualified view.
Comparing a receipt to its own saved process metadata is insufficient. Compiler
and health readers require the saved instance/readback to match that view and
the full supplied executing JSON. This input is separate from the stored panel
template: stock adds DB inbounds, subscription outbounds and injected rules.
Generated state is never copied back into the mutation template. A raw on-disk
config/PID pair is insufficient after hot apply: stock can fail to persist its
changed config while the engine has already changed.

Each fresh sample still binds the complete current executing digest. Within
the same process, accumulated hysteresis may survive a change to exactly the
owned rule's `outboundTag`. The compiler first validates the unique unconditional
rule, exact inbound, approved target and all prepared outbounds, EU guards,
ordering and selector collisions, then replaces only this one selector with
blackhole in a copy of the full executing JSON to derive a continuity hash.
All other fields, generated inbounds, array ordering and new processes reset
history. This prevents recovery → selector update → hash reset → block loops.
The derived hash is stored only in the existing private runtime slot; it never
qualifies a new sample, settles a mutation or refreshes an old timestamp.
Heartbeat/replay cannot extend collection-start freshness.

No-op comparison checks owned rule/balancer/path/observatory intent against the
policy-validated full execution. Unrelated stock-generated objects are retained
in execution without forcing template rewrites. Their changes still reset
accumulated health. Public descriptors and private observation schema2 are
unchanged by this separation.

The pure compiler/health/no-op checks and default-closed CLI boundary are
implemented locally. A no-op routine tick updates only the atomic health file;
an unchanged engine template is never POSTed just to refresh evidence. An actual
POST retains its pending-effect guard even after HTTP success and cache readback.
No pre-apply receipt is committed as post-apply health. A config-only change
currently stops before POST pending coordinated metadata handling.

PR2 confirmed that the production process/config collector does NOT yet exist;
PID/start_ticks collection so far is a native-fixture mechanism. Observation
production, executing-config qualification, pending-transition settlement and
collector/Agent/SaaS integration remain actual dependencies. A mutable panel
GetConfig/cache is not an authoritative executing-config view. The historical native fixture
and its schema1 observations do not qualify this new process-bound contract.
Do not use this working draft as an installed/runtime acceptance release.

## Agent integration: current stock result is unqualified

The existing Agent observation cycle adds `state.prepared_eu_paths` only through
its current primary mTLS transport. It keeps the complete existing health state,
`node.health.*` event type and `node-health-*` idempotency key. The SaaS consumer
replaces the whole observed state, so a separate prepared-only POST would erase
the previous health fields and is forbidden.

For the traced stock interfaces the receipt is explicitly `unqualified`, with
`readback=unavailable`, `deny_outcome=not_verified`, no executing instance/digest,
no selected path and null sample timestamps/public profile. A configured
prepared route always advertises `prepared_eu_paths_v1=false` in this stage.
Only a canonical hash of the private desired plan is included; raw plans,
Exit addresses and relay material never enter the observation. Cached positives
cannot be replayed through this negative producer. Legacy bearer health and
access lifecycle transport retain their existing paths; prepared evidence does
not downgrade to bearer.

Before sending, the Agent saves the complete health envelope, Node/tenant scope
and actual idempotency key in its existing private runtime file. Both the
`node.health.*` type suffix and `node-health-*` key bind Node, tenant, configured
observed version and full-state hash. This also avoids collision with an older
Agent's already accepted state-only observation at the same version. A lost
response retries the exact saved body, timestamp and key after restart; a newer
configuration never rewrites an unknown old envelope's identity. Identical
accepted state deduplicates only within the same Node/tenant/version.

The SaaS rejects an older observation version before duplicate readback. Only
an authenticated409 `STALE_OBSERVED_VERSION`, when the actual configured version
is strictly newer, may supersede that pending message. The Agent first saves
one bounded resolved receipt, retaining the original envelope/key and the fact
that its first dispatch may have been accepted. The next existing cycle can
then collect a new current full envelope. Other errors or a same-version stale
response retain pending debt; no server error can invent a configured version.
A missing saved key remains closed. Errors use fixed codes without raw private
exception text. The observed Node version is not increased for PR4.

This implements negative reporting and transport recovery only. It is **not**
the positive execution collector, transition settlement or dataplane watchdog.
Stock 3x-ui `GetConfigJson` reconstructs desired state, while the pinned Xray
`ListRule` returns only tags, not rule conditions. Positive implementation needs
an explicitly agreed native execution guarantee; a controlled cold-generation
and sole-writer option must also account for every stock/CLI/native writer and
for the process reset caused by changing the selected route. No vendor fork,
native mutator change, public-profile projection or financial fence change is
introduced by this negative integration.
