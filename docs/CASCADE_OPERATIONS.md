# Cascade VPN operations

This runbook covers the supported Entry and Exit lifecycle for WaveMesh cascade routing. Run commands as root or with `sudo`. Keep manifests, API tokens, UUIDs, subscription URLs, and transaction snapshots out of tickets and chat logs.

## Topology and prerequisites

Use one public Entry and at least two public Exits for the full E2E test:

```text
Client -> Entry nginx:443 -> Entry Xray route -> Exit nginx:443 -> Exit Xray -> Internet
```

Each node requires Ubuntu/Debian, a unique public domain, correct DNS, and provider firewall access to `22/tcp`, `80/tcp`, and `443/tcp`. Panel, route, relay, and Xray API ports must remain loopback-only.

Record non-secret identifiers before starting:

```text
Entry node id:       ru-msk-1
Entry public IP:     ENTRY_PUBLIC_IP
First Exit node id:  de-fra-1
Second Exit node id: nl-ams-1
```

## Install the Entry

On the Entry VPS:

```bash
git clone https://github.com/Egorius91/wavemesh-node-builder.git
cd wavemesh-node-builder
git switch feature/cascade-vpn

sudo bash install.sh \
  --role entry \
  --node-id ru-msk-1 \
  --country RU \
  --city Moscow \
  --domain entry.example.com \
  --email admin@example.com \
  --clients 1
```

Verify the base node before importing an Exit:

```bash
sudo systemctl is-active x-ui nginx
sudo wavemesh diagnostics
sudo wavemesh cascade status --json
```

## Install an Exit

On each Exit VPS, use a distinct node id and domain:

```bash
git clone https://github.com/Egorius91/wavemesh-node-builder.git
cd wavemesh-node-builder
git switch feature/cascade-vpn

sudo bash install.sh \
  --role exit \
  --node-id de-fra-1 \
  --country DE \
  --city Frankfurt \
  --domain exit-de.example.com \
  --email admin@example.com \
  --clients 1
```

Verify that 3X-UI and nginx are active and that the panel binds only to loopback:

```bash
sudo systemctl is-active x-ui nginx
sudo wavemesh diagnostics
sudo ss -ltnp
```

## Create and transfer a join manifest

On the Exit, create one peer for the Entry. The output file is secret and is created with mode `0600`:

```bash
sudo wavemesh exit peer create \
  --entry-id ru-msk-1 \
  --entry-ip ENTRY_PUBLIC_IP \
  --display-name "RU Moscow Entry" \
  --output /root/de-fra-1.join.json
```

Confirm the file without printing it:

```bash
sudo stat -c 'mode=%a bytes=%s path=%n' /root/de-fra-1.join.json
sudo wavemesh exit peer list
```

Transfer it through an authenticated administrative channel such as `scp` directly between the nodes. Do not paste it into chat, email, issue trackers, or shell history as inline JSON.

```bash
scp root@EXIT_HOST:/root/de-fra-1.join.json /root/de-fra-1.join.json
chmod 600 /root/de-fra-1.join.json
```

Delete both transferred copies after the route is verified or retain one only in an encrypted secret store.

## Import an Exit on the Entry

First inspect the plan, then apply it:

```bash
sudo wavemesh cascade add-exit \
  --manifest /root/de-fra-1.join.json \
  --display-name "RU -> Germany" \
  --sort-order 100 \
  --dry-run

sudo wavemesh cascade add-exit \
  --manifest /root/de-fra-1.join.json \
  --display-name "RU -> Germany" \
  --sort-order 100
```

The import is transactional. It creates the managed inbound, outbound and routing rule, verifies `testOutbound` and `routeTest`, updates nginx and subscriptions, then commits desired state. Repeating the same import is a no-op.

Run health three times because state transitions require consecutive observations:

```bash
sudo wavemesh cascade health --json
sudo wavemesh cascade health --json
sudo wavemesh cascade health --json
sudo wavemesh subscription validate
```

## Add the second Exit

Install the second Exit using its own id and domain, then create its manifest:

```bash
sudo wavemesh exit peer create \
  --entry-id ru-msk-1 \
  --entry-ip ENTRY_PUBLIC_IP \
  --display-name "RU Moscow Entry" \
  --output /root/nl-ams-1.join.json
```

Transfer it to the Entry and import it with a distinct display name and sort order:

```bash
sudo wavemesh cascade add-exit \
  --manifest /root/nl-ams-1.join.json \
  --display-name "RU -> Netherlands" \
  --sort-order 200

sudo wavemesh cascade health --json
sudo wavemesh cascade health --json
sudo wavemesh cascade health --json
sudo wavemesh subscription validate
sudo wavemesh cascade verify-e2e --json
```

`verify-e2e` succeeds only when at least two distinct enabled Exits are healthy, every enabled client has both profiles, each `routeTest` selected its expected outbound, and installed subscription files contain only Entry-facing connection data. `subscription validate` separately checks the public HTTP result. Both commands keep output redacted.

## Verify profiles from a client

Refresh the existing Entry subscription in the VPN client. It must show both display names in configured sort order. Test one profile at a time:

1. Connect to `RU -> Germany` and check the external IP belongs to the German Exit.
2. Disconnect, connect to `RU -> Netherlands`, and check the external IP belongs to the Netherlands Exit.
3. Confirm both profiles still connect to the Entry domain on port 443.

Do not publish the subscription URL or decoded VLESS lines as evidence. Record only the selected profile name, observed country/ASN, time, and pass/fail result.

## Disable, enable, and remove

List route ids:

```bash
sudo wavemesh route list --json
```

Disable and later re-enable a route without deleting its Exit:

```bash
sudo wavemesh route disable --route-id route-de-fra-1
sudo wavemesh route enable --route-id route-de-fra-1
```

Remove only a route:

```bash
sudo wavemesh route remove --route-id route-de-fra-1
```

Remove an Exit with no attached route:

```bash
sudo wavemesh cascade remove-exit --exit-id de-fra-1
```

`--force` removes attached routes as one transaction and should be used only after a backup and maintenance announcement:

```bash
sudo wavemesh cascade remove-exit --exit-id de-fra-1 --force
```

Remove the corresponding relay peer on the Exit after the Entry route is gone:

```bash
sudo wavemesh exit peer remove --entry-id ru-msk-1
```

## Rotate relay credentials

The current CLI deliberately refuses to overwrite an existing Exit id with different credentials. The safest rotation is replacement-first:

1. Prepare a replacement Exit id, for example `de-fra-2`.
2. Create and transfer a new manifest.
3. Import and verify the replacement profile.
4. Disable the old route and confirm clients use the replacement.
5. Remove the old route, Exit, and relay peer.

This avoids an interval where every client loses the country profile. Reusing the same Exit id requires a maintenance window: remove the old Entry route/Exit and Exit peer, create a fresh peer, then import the new manifest.

## Backup and restore

Every mutating command creates a private transaction snapshot under `/etc/wavemesh-node/transactions`. Check transaction state before maintenance:

```bash
sudo wavemesh transaction list --json
```

For an additional node backup, save config/runtime, managed nginx, subscriptions, and an online SQLite backup to a root-only directory. Do not copy a live SQLite main file directly:

```bash
backup="/root/wavemesh-backup-$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -d -m 0700 "$backup"
sudo install -m 0600 /etc/wavemesh-node/config.json "$backup/config.json"
sudo install -m 0600 /etc/wavemesh-node/runtime.json "$backup/runtime.json"
sudo install -m 0600 /etc/nginx/wavemesh-managed-locations.conf "$backup/nginx.conf"
sudo cp -a /var/www/wavemesh-sub "$backup/subscriptions"
sudo python3 - "$backup/x-ui.db" <<'PY'
import json
import os
import sqlite3
import sys

config = json.load(open("/etc/wavemesh-node/config.json", encoding="utf-8"))
source_path = config.get("installation", {}).get("xui", {}).get("database_path")
if not source_path:
    raise SystemExit("configured 3X-UI database path is missing")
source = sqlite3.connect(source_path)
target = sqlite3.connect(sys.argv[1])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
os.chmod(sys.argv[1], 0o600)
PY
sudo chmod -R go-rwx "$backup"
echo "backup=$backup"
```

For disaster recovery, restore only a backup from the same node and version. Stop `x-ui` before replacing its database, validate JSON, run `nginx -t`, restart services, and run reconciliation plus subscription validation before reopening traffic.

## Health and drift

Use these commands for normal operations:

```bash
sudo wavemesh cascade status --json
sudo wavemesh cascade health --json
sudo wavemesh reconcile --dry-run
sudo wavemesh subscription validate
sudo wavemesh transaction list --json
```

Apply managed drift repair only after reviewing the dry-run plan:

```bash
sudo wavemesh reconcile --apply
```

## Upgrade 3X-UI

Treat a 3X-UI upgrade as a maintenance operation:

1. Save the WaveMesh node backup and record the installed version.
2. Verify the target release still exposes the required inbound, Xray update, `testOutbound`, and `routeTest` API paths.
3. Upgrade using the upstream-supported package or installer path.
4. Confirm `x-ui` is active and the panel remains loopback-only.
5. Run `wavemesh cascade health --json` three times, `subscription validate`, and `cascade verify-e2e --json`.

Do not fall back to direct SQLite mutation when Bearer API verification fails.

## Recovery after interruption

An interrupted transaction blocks later mutations:

```bash
sudo wavemesh transaction list --json
sudo wavemesh transaction recover --latest
sudo wavemesh subscription validate
sudo wavemesh cascade health --json
```

If recovery reports `rollback_failed`, stop mutating commands. Preserve the transaction directory, inspect only redacted status and service logs, repair the reported external prerequisite, then retry recovery or follow the matching troubleshooting section.
