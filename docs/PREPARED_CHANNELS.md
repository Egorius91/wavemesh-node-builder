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
wavemesh cascade auto prepared install --id europe --plan /protected/plan.json
wavemesh cascade auto prepared reconcile --id europe
```

These are source interface examples, not authorization to run on a VPS. Install
requires an unpublished, materialized Auto Route and starts with its entire
client inbound blocked. Reconcile checks every prepared full VLESS/XHTTP/TLS
outbound through existing `testOutbound(mode=real)`, then selects only one
admitted path. The backup outbounds already exist before failure. There is no
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

Operations keep the existing lock → snapshot → compile/native parse → mutation
→ full-template readback → validation → atomic config/runtime commit flow.
The saved config, effective template and stored SQLite template must prove a
safe recovery policy before restore. Missing rules for an active saved Entry
or an unsafe fallback refuse rollback. Existing durable `rollback_failed` debt
blocks another mutation until recovery is resolved. An old live Auto Route
without a provable EU-only snapshot is deliberately refused before mutation;
its migration needs a separately reviewed fail-closed transition. Returning to
an unsafe historical template is not an upgrade strategy.

Before a prepared Xray POST, the transaction persists the candidate hash and a
pending-effect guard. Only a proven successful terminal HTTP response clears
it. Lost/failed replies or interruption leave the guard, stop automatic restore
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
