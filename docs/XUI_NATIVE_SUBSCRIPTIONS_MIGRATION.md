# Native 3X-UI subscriptions

New installations use `network.subscription.backend=xui-native`. Existing version 2 configurations without a backend are classified from their current state: custom per-client metadata, generated mode, static files, or managed exact locations select `wavemesh-renderer`; an already-native layout selects `xui-native`. Installing updated code does not replace a detected renderer layout with native publication.

## Inspect

```bash
sudo wavemesh subscription backend status
sudo wavemesh subscription backend switch xui-native --dry-run
```

The native backend requires the pinned 3X-UI release, the first-class Clients API, and a subscription listener bound only to `127.0.0.1:2096`.

## Switch

```bash
sudo wavemesh subscription backend switch xui-native --apply
sudo wavemesh subscription validate-native
```

The switch snapshots `x-ui.db`, WaveMesh configuration, nginx configuration, runtime state, and existing subscription files. The custom files remain on disk but are no longer published. Native mode exposes one nginx subscription namespace and does not render per-client static locations.

After switching, update the bot to use the active base URL and let it create the same `email` and `subId` on every published inbound. Inbounds whose remark starts with `--!` are internal and must be ignored by the bot.

Validate a known test user without printing its subscription:

```bash
sudo wavemesh subscription validate-native \
  --sub-id TEST_SUB_ID \
  --expected-profiles 3
```

## Roll back

```bash
sudo wavemesh subscription backend rollback
```

Rollback restores the previous database, configuration, nginx files, and subscription files, then verifies the restored backend.

The fallback renderer remains available explicitly:

```bash
sudo wavemesh subscription backend switch wavemesh-renderer --apply
```
