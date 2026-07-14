# Phase 11.2 — persistent Auto Route runtime

Status: implemented for review; public subscription exposure remains disabled.

This increment turns the Phase 11.1 balancer renderer into a transactional Entry-node operation.

## Added behavior

- `wavemesh cascade auto create` creates persistent desired state for one Auto Route.
- The command creates a dedicated loopback VLESS/XHTTP inbound with per-client credentials.
- A `leastPing` Xray balancer is rendered from exact enabled `wm-exit-*` outbounds.
- The routing rule uses `balancerTag`, never `outboundTag`, and is inserted before catch-all rules.
- Observatory settings are installed for the selected Exit outbounds.
- Xray configuration is read back and verified after apply.
- The operation uses the existing mutation lock, transaction snapshots, rollback, and atomic config installation.
- `wavemesh cascade auto status [--json]` reports persistent Auto Route state without secrets.

## Safety boundary

The generated route contains:

```json
"presentation": {"published": false}
```

Therefore this increment does **not**:

- add an nginx public location for the Auto inbound;
- render an Auto profile into user subscriptions;
- alter existing manual country routes;
- introduce a direct/freedom fallback.

The dedicated inbound and balancer can be validated on a test Entry before public exposure is implemented.

## Commands

Create an unpublished Auto Route from every enabled Exit:

```bash
sudo wavemesh cascade auto create
```

Select specific Exits:

```bash
sudo wavemesh cascade auto create \
  --id auto-europe \
  --display-name '⚡ RU -> Auto Europe' \
  --exit-id de-fra-1 \
  --exit-id de-frankfurt-2
```

Preview only:

```bash
sudo wavemesh cascade auto create --dry-run
```

Inspect persistent state:

```bash
sudo wavemesh cascade auto status
sudo wavemesh cascade auto status --json
```

## Acceptance for this increment

- creation is rejected outside an Entry node;
- at least one enabled Exit is required;
- disabled or unknown Exit ids are rejected;
- duplicate Auto ids are rejected;
- the Auto inbound remains loopback-only;
- the applied balancer and rule survive read-back verification;
- existing manual route subscriptions remain byte-for-byte unchanged;
- an apply failure rolls back through the existing transaction layer.

## Next increment

Phase 11.3 will add runtime health states, balancer status/override commands, and controlled enable/disable lifecycle. Public nginx and subscription exposure will follow only after the control plane is live-verified.
