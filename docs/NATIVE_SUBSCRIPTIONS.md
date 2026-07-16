# 3X-UI native subscriptions

WaveMesh uses the 3X-UI subscription server as the source of truth on new
Entry and standalone nodes. nginx exposes its loopback listener through the
opaque path stored in `network.subscription.path`.

## Backends

- `xui-native`: 3X-UI owns subscription content and client associations.
- `generated`: the previous `/var/www/wavemesh-sub` renderer, retained for
  rollback and existing nodes until migration.

Node Builder owns inbound, route, nginx and presentation state. It preserves
clients which are not explicitly present in its canonical config. Hidden or
internal inbounds use a `--!` remark and contain no public client associations.

## Existing node migration

Create a node backup before migration, then run:

```bash
sudo wavemesh subscription migrate-native --dry-run
sudo wavemesh subscription migrate-native --apply
sudo wavemesh subscription capabilities --json
sudo wavemesh subscription validate
```

The apply operation snapshots WaveMesh config, runtime state, nginx, generated
subscription files and the 3X-UI SQLite database. It establishes missing
`subId` values only for clients matched by builder-owned UUIDs. Existing
non-empty native IDs are adopted instead of overwritten.

The transaction rolls back automatically if settings read-back, nginx reload,
Clients API comparison or public content validation fails.

After a successful migration, an operator can deliberately restore the old
renderer without changing client IDs:

```bash
sudo wavemesh subscription fallback-generated --dry-run
sudo wavemesh subscription fallback-generated --apply
```

## Acceptance

The JSON capability report must contain:

```json
{
  "backend": "xui-native",
  "clients_api": true,
  "settings_api": true,
  "native_listener_loopback": true,
  "custom_renderer_locations": false,
  "ready": true
}
```

Do not publish subscription URLs, client UUIDs, `subId` values, API tokens or
decoded profile links in shared logs.
