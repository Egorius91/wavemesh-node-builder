# Phase 11 — Auto Route

Status: in progress.

## Goal

Add a persistent Auto Route data model and deterministic Xray balancer rendering without exposing an Auto profile publicly yet.

The first increment is intentionally control-plane only. Existing manual cascade routes and subscriptions remain unchanged.

## Scope of the first increment

- optional top-level `balancers` array in config schema v2;
- support `kind: auto` route records in schema v2;
- deterministic managed Xray balancer rendering;
- `leastPing` as the only accepted strategy;
- observatory rendering for selected managed Exit outbounds;
- Auto routing rule inserted before catch-all rules;
- idempotent update and duplicate prevention;
- removal of the managed balancer, rule, and unused observatory;
- unit coverage for valid rendering, idempotency, missing selectors, empty selectors, unsupported strategies, catch-all ordering, and removal.

## Safety invariants

1. Auto Route selectors must reference exact existing `wm-exit-*` outbounds.
2. Empty selectors are rejected.
3. Missing selector targets are rejected before writing a candidate template.
4. Only `leastPing` is accepted in this increment.
5. The Auto routing rule uses `balancerTag`; it must never use `outboundTag` or fall back to `freedom`/`direct`.
6. Managed Auto rules are inserted before the first catch-all rule.
7. Repeated rendering must produce one balancer and one routing rule.
8. This increment does not create a public Auto inbound or subscription profile.

## Managed object conventions

```text
Inbound tag:    wm-route-auto-<id>
Balancer tag:   wm-balancer-<id>
Rule tag:       wm-rule-auto-<id>
Exit selectors: wm-exit-<exit-id>
```

## CLI adapter added to xray_template.py

```bash
python3 scripts/lib/xray_template.py merge-balancer \
  --template current.json \
  --selectors wm-exit-de-fra-1,wm-exit-de-frankfurt-2 \
  --inbound-tag wm-route-auto-europe \
  --balancer-tag wm-balancer-auto-europe \
  --rule-tag wm-rule-auto-europe \
  --strategy leastPing \
  --output candidate.json
```

Removal:

```bash
python3 scripts/lib/xray_template.py remove-balancer \
  --template current.json \
  --inbound-tag wm-route-auto-europe \
  --balancer-tag wm-balancer-auto-europe \
  --rule-tag wm-rule-auto-europe \
  --output candidate.json
```

## Next increment

After this PR is live-tested against a real 3X-UI/Xray template:

1. add persistent config mutation commands for Auto Route;
2. create and reconcile a dedicated Auto inbound;
3. integrate balancer status and override APIs;
4. add Auto health states (`healthy`, `degraded`, `unhealthy`, `misconfigured`, `disabled`);
5. add the `⚡ RU → Auto Europe` profile to private client subscriptions;
6. verify that no direct RU fallback is possible when all Exit targets are unavailable.
