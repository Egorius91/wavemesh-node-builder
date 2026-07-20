# Phase 11.6 — Auto-only subscription publication

## Goal

Keep every manual cascade route operational for health checks and for the Auto Route `leastPing` balancer, while exposing only the published Auto profile to VPN clients.

This phase does not modify Exit nodes, relay credentials, managed outbounds, or the Auto Route balancer.

## Publication modes

The Entry configuration uses `network.subscription.publication_mode`:

- `all` — backward-compatible default. Enabled manual cascade routes and explicitly published Auto Routes are public.
- `auto-only` — only enabled Auto Routes with `presentation.published=true` are public.

In `auto-only` mode manual route inbounds remain enabled but receive a hidden `--!` remark. Their nginx locations are removed and generated/native subscriptions omit them.

For the `xui-native` backend, 3X-UI builds subscriptions from client-to-inbound attachments and does not use the `--!` remark as a native subscription filter. Client attachments are therefore removed from hidden manual inbounds during the transactional apply. The global client remains attached to the published Auto inbound. The WaveMesh bot enforces the same visibility policy and will reattach clients when a route becomes public again.

## Safety preflight

For the `xui-native` backend, switching to `auto-only` checks every enabled native subscription identity found on routes that will become hidden. Each identity must already exist on every published Auto inbound. The command stops before changing state when profiles are missing.

This prevents existing users from receiving an empty subscription when manual attachments are removed. Synchronize active keys through the bot, then repeat the dry run.

No subscription IDs, UUIDs, or email addresses are printed by the preflight; only aggregate counts are shown.

## Commands

Show the current state:

```bash
wavemesh subscription publication-mode
wavemesh subscription publication-mode --json
```

Preview Auto-only publication:

```bash
wavemesh subscription publication-mode auto-only --dry-run
```

Apply after the dry run succeeds:

```bash
wavemesh subscription publication-mode auto-only --apply
```

Rollback to manual plus Auto profiles:

```bash
wavemesh subscription publication-mode all --dry-run
wavemesh subscription publication-mode all --apply
```

The apply operation is transactional. It snapshots the Entry configuration, 3X-UI database, nginx state, generated subscriptions, and runtime state. A failed reconciliation or validation restores the client attachments and all other managed state from the snapshot.

After a committed change back to `all`, run the bot subscription materializer so clients are reattached to newly public manual inbounds without waiting for the next scheduled synchronization.

## Validation

After apply:

```bash
wavemesh subscription validate
wavemesh cascade auto status --json
wavemesh cascade auto health --json
wavemesh cascade verify-e2e --json
wavemesh cascade health --json
```

Expected client result: one `Auto → Europe` profile when exactly one Auto Route is published. Manual country/server profiles must be absent, while manual route health and Auto `leastPing` selectors remain healthy.
