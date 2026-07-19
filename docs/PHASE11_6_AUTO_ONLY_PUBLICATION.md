# Phase 11.6 — Auto-only subscription publication

## Goal

Keep every manual cascade route operational for health checks and for the Auto Route `leastPing` balancer, while exposing only the published Auto profile to VPN clients.

This phase does not modify Exit nodes, relay credentials, managed outbounds, or the Auto Route balancer.

## Publication modes

The Entry configuration uses `network.subscription.publication_mode`:

- `all` — backward-compatible default. Enabled manual cascade routes and explicitly published Auto Routes are public.
- `auto-only` — only enabled Auto Routes with `presentation.published=true` are public.

In `auto-only` mode manual route inbounds remain enabled but receive a hidden `--!` remark. Their nginx locations are removed and generated/native subscriptions omit them. Existing client records on those hidden inbounds are preserved so switching back to `all` is reversible and does not delete clients owned by the bot.

## Safety preflight

For the `xui-native` backend, switching to `auto-only` checks every enabled native subscription identity found on routes that will become hidden. Each identity must already exist on every published Auto inbound. The command stops before changing state when profiles are missing.

This prevents existing users from receiving an empty subscription after manual profiles are hidden. Synchronize active keys through the bot, then repeat the dry run.

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

The apply operation is transactional. It snapshots the Entry configuration, 3X-UI database, nginx state, generated subscriptions, and runtime state. A failed reconciliation or validation triggers rollback.

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
