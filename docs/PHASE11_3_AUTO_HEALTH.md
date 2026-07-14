# Phase 11.3 — Auto Route health and lifecycle

This increment adds operational control for an unpublished Auto Route.

Commands:

```bash
wavemesh cascade auto health [--json]
wavemesh cascade auto enable [--id ID]
wavemesh cascade auto disable [--id ID]
wavemesh cascade auto override [--id ID] --exit-id EXIT_ID
wavemesh cascade auto override [--id ID] clear
```

Health states:

- `healthy`: every selected Exit is healthy and the inbound, balancer, rule, observatory, and selector outbounds match desired state;
- `degraded`: at least one selected Exit is healthy, but another selected Exit is not healthy;
- `unhealthy`: no selected Exit is healthy and all selected Exit states are known;
- `misconfigured`: managed Xray or inbound structure differs from desired state;
- `disabled`: the Auto Route is intentionally disabled;
- `unknown`: Exit health has not yet converged.

Manual override is stored outside `config.json` in `/etc/wavemesh-node/auto-overrides.json`. Clearing the override restores the configured selector set. No direct or `freedom` fallback is introduced.

The Auto profile remains unpublished in this increment. Nginx and public subscription files are not changed.
