# VPS testing guide

This guide is for testing WaveMesh Node Builder on a clean VPS while the repository is public.

## 1. Prepare VPS

Use a clean Ubuntu/Debian server.

Open provider firewall ports:

```text
22/tcp
80/tcp
443/tcp
```

Point a test domain A-record to the VPS public IPv4.

## 2. Clone public repository

```bash
git clone https://github.com/Egorius91/wavemesh-node-builder.git
cd wavemesh-node-builder
```

For an existing checkout:

```bash
cd ~/wavemesh-node-builder
git pull
```

## 3. Run installer

Replace the domain and email:

```bash
sudo bash install.sh --domain test.example.com --email admin@example.com --clients 1
```

The installer should now automate the 3X-UI v3.4.x flow:

```text
Install
SQLite
custom generated panel port
generated username/password
generated web base path
Skip internal SSL
Bind panel to 127.0.0.1
```

3X-UI must not be configured with its own SSL because nginx terminates TLS externally.

## 4. Expected results

After the installer completes:

```text
https://test.example.com/              -> Web Identity website
https://test.example.com/sub/<random>/ -> public subscription URL
```

Internally:

```text
3X-UI service: active
3X-UI panel: 127.0.0.1:<generated-panel-port>/<generated-path>/
XHTTP inbound target: 127.0.0.1:<generated-xhttp-port>
```

The subscription must contain client links with:

```text
address: test.example.com
port: 443
security: tls
type: xhttp
path: generated /api/<random>/ path
sni: test.example.com
host: test.example.com
```

It must not contain:

```text
127.0.0.1
localhost
server public IP
XHTTP local port
3X-UI panel port
```

## 5. Run diagnostics

```bash
sudo wavemesh show-report
sudo wavemesh diagnostics
sudo wavemesh validate-subscription
```

Also check the canonical config:

```bash
sudo cat /etc/wavemesh-node/config.json
```

It should include:

```json
"installation": {
  "xui": {
    "database": "sqlite",
    "panel_bind": "127.0.0.1",
    "ssl_mode": "external-nginx"
  }
}
```

## 6. Inspect 3X-UI API/inbound state

```bash
sudo systemctl status x-ui --no-pager
sudo journalctl -u x-ui -n 100 --no-pager
sudo ss -ltnp | grep x-ui
sudo x-ui settings
sudo find /etc /usr/local -maxdepth 4 -type f -name '*x-ui*.db'
```

If a database exists:

```bash
sudo sqlite3 /path/to/x-ui.db '.tables'
sudo sqlite3 /path/to/x-ui.db '.schema'
sudo sqlite3 /path/to/x-ui.db 'select * from inbounds;'
```

## 7. What to send back after test

Send:

```text
- installer output around 3X-UI install;
- /etc/wavemesh-node/config.json without secrets if sharing publicly;
- x-ui service status;
- x-ui settings with secrets masked;
- ss -ltnp line for panel port;
- sqlite .tables and .schema output;
- inbounds row after API attempt.
```

Do not share real panel passwords, tokens, private keys, or UUIDs publicly.

## 8. Repository tests

Run the deterministic two-Exit E2E test together with the unit and adapter suites:

```bash
python3 tests/e2e/test_multi_exit.py
python3 tests/unit/test_subscription_renderer.py
python3 tests/unit/test_subscription_backend.py
python3 tests/unit/test_runtime_state.py
python3 tests/unit/test_xray_template.py
bash tests/unit/test_transaction.sh
bash tests/integration/test_xui_api.sh
```

The E2E fixture creates two independent Exit manifests, imports both into one
Entry desired state, merges two Xray outbounds/rules, renders one subscription
with two ordered profiles, rejects Exit secret leakage, and runs the same
redacted verifier used on a live Entry.

## 9. Live two-Exit acceptance

Follow [`docs/CASCADE_OPERATIONS.md`](docs/CASCADE_OPERATIONS.md) to install and
import two distinct Exits. After three health observations, run:

```bash
sudo wavemesh subscription validate
sudo wavemesh cascade verify-e2e --json
```

Then connect a client to each profile separately and record only the profile
name, observed country/ASN, timestamp, and pass/fail result. Never paste the
subscription URL or decoded VLESS lines.

## 10. Native 3X-UI subscription acceptance

On a new installation, verify the selected backend and the loopback-only native
listener:

```bash
sudo wavemesh subscription backend status
sudo ss -ltnp | grep ':2096 '
sudo wavemesh routes public --json
sudo wavemesh subscription validate-native
```

For a known 3X-UI client `subId`, perform the full public-content check without
printing the subscription body:

```bash
sudo wavemesh subscription validate-native \
  --sub-id 'CLIENT_SUB_ID' \
  --expected-profiles 3
```

Before migrating an existing node, inspect the dry-run. Apply only after it
reports the expected domain and opaque path:

```bash
sudo wavemesh subscription backend switch xui-native --dry-run
sudo wavemesh subscription backend switch xui-native --apply
```

If the post-migration bot check fails, restore the captured pre-switch state:

```bash
sudo wavemesh subscription backend rollback
```
