# WaveMesh Node Builder

Bash-based installer/wizard for quickly deploying a WaveMesh node on a clean Ubuntu/Debian VPS.

MVP goals:

- create a unique multi-page Web Identity cover website;
- configure nginx and Let's Encrypt SSL;
- install 3X-UI;
- create VLESS + XHTTP topology through nginx;
- configure 3X-UI native multi-inbound subscriptions behind an opaque public URL;
- validate that subscriptions never expose local ports, localhost, panel port, or raw IP when a domain is configured.

> This project is original WaveMesh infrastructure code. It does not copy private goVLESS source code. goVLESS is treated only as a behavioral reference.

## Network model

Public ports:

- `80/tcp` for Let's Encrypt HTTP-01;
- `443/tcp` for website, XHTTP and subscription.

Internal-only ports:

- 3X-UI panel port;
- Xray XHTTP inbound port;
- optional local subscription service port.

```text
Client
  -> https://domain.com:443/<random-xhttp-path>/
  -> nginx TLS termination
  -> http://127.0.0.1:<random-xhttp-local-port>
  -> Xray inbound
```

Subscription URL:

```text
https://domain.com/<opaque>/<opaque>/<subId>
```

Every client link inside the subscription must use:

```text
domain.com:443 + security=tls + type=xhttp + correct xhttp path
```

## Quick start

```bash
sudo bash install.sh --domain example.com --email admin@example.com
```

Interactive wizard is also supported and asks for the minimum install inputs:

- domain name;
- Let's Encrypt email, optional;
- Pexels API key, optional;
- cover-site theme, 10 choices or auto.

```bash
sudo bash install.sh
```

Non-interactive theme selection:

```bash
sudo bash install.sh \
  --domain example.com \
  --email admin@example.com \
  --site-theme coffee \
  --pexels-key YOUR_PEXELS_KEY
```

Available themes: `auto`, `logistics`, `architecture`, `coffee`, `energy`, `legalops`,
`studio`, `wellness`, `education`, `finance`, `gardening`.

## 3X-UI installation layer

The installer now has a real 3X-UI installation layer in `scripts/06_3xui.sh`.

It uses a configurable upstream installer URL:

```bash
XUI_INSTALL_URL="https://raw.githubusercontent.com/MHSanaei/3x-ui/master/install.sh"
```

You can override it when testing:

```bash
sudo XUI_INSTALL_URL="https://example.com/install.sh" bash install.sh --domain example.com --email admin@example.com
```

The script attempts non-interactive setup with generated panel credentials and path, then detects the 3X-UI database location and creates a backup before future database/API work.

## Files created on a node

```text
/etc/wavemesh-node/config.env
/etc/wavemesh-node/config.json
/etc/wavemesh-node/runtime.json
/etc/wavemesh-node/report.json
/root/wavemesh-node-report.txt
/root/wavemesh-node-report.json
/var/www/wavemesh-site
/var/www/wavemesh-sub
```

## Web Identity cover site

The installer generates a small realistic multi-page website in `/var/www/wavemesh-site`.
It is seeded from the domain and generated brand, so fresh nodes do not all look alike.

Generated pages and assets:

```text
index.html
about.html
services.html
contact.html
assets/style.css
assets/site.js
assets/img/hero.svg
assets/img/detail.svg
favicon.svg
robots.txt
sitemap.xml
```

The generator uses document-relative asset paths, does not depend on external fonts/CDNs,
and never touches `/var/www/certbot` or the installed TLS certificate.

For real topical photos, pass a Pexels API key at install time:

```bash
sudo bash install.sh --domain example.com --email admin@example.com --pexels-key YOUR_PEXELS_KEY
```

You can also use an environment variable:

```bash
sudo PEXELS_API_KEY=YOUR_PEXELS_KEY bash install.sh --domain example.com --email admin@example.com
```

The key is used only during site generation and is not written to `config.env` or reports.
If the API is unavailable, the generator falls back to local SVG images.

## CLI

After installation:

```bash
wavemesh show-report
wavemesh diagnostics
wavemesh validate-subscription
wavemesh subscription capabilities --json
wavemesh subscription migrate-native --dry-run
wavemesh subscription migrate-native --apply
wavemesh cascade status --json
wavemesh cascade health [--exit-id EXIT_ID] [--json]
wavemesh cascade verify-e2e [--json]
wavemesh route list [--json]
wavemesh route enable --route-id ROUTE_ID
wavemesh route disable --route-id ROUTE_ID
wavemesh route remove --route-id ROUTE_ID
wavemesh cascade remove-exit --exit-id EXIT_ID [--force]
wavemesh reconcile --dry-run
wavemesh reconcile --apply
wavemesh transaction list [--json]
wavemesh transaction recover --id TRANSACTION_ID
wavemesh transaction recover --latest
wavemesh repair --nginx
wavemesh repair --ssl
wavemesh repair --subscriptions
```

## Current MVP status

Implemented:

- argument parsing;
- root/system checks;
- random paths and ports;
- firewall baseline;
- BBR enablement;
- multi-page Web Identity cover-site generation;
- nginx/SSL templates;
- configurable 3X-UI installation layer;
- 3X-UI database discovery and backup;
- standalone, Entry, and Exit roles;
- managed Entry-to-Exit VLESS/XHTTP cascade routes;
- private per-client native multi-route subscriptions with the generated renderer retained for rollback;
- route lifecycle and forced Exit removal commands;
- persisted `runtime.json` health state with three-check thresholds;
- redacted desired/observed drift detection and managed reconciliation;
- interruption-safe mutation transactions with explicit recovery and bounded retention;
- redacted two-Exit E2E verification for route selection and subscription profiles;
- GitHub Actions coverage for Python, Bash, adapter, transaction, and two-Exit E2E tests;
- strict validation;
- report generation;
- CLI wrapper.

Every mutating command records an `in_progress` transaction under
`/etc/wavemesh-node/transactions`. A failed command rolls back automatically. A
process kill or reboot leaves the transaction discoverable and blocks later
mutations until `wavemesh transaction recover --id ID` (or `--latest`) completes
the restore and post-rollback checks. Terminal transactions and each backup
family retain the newest 20 entries by default.

Operator documentation:

- [`docs/CASCADE_OPERATIONS.md`](docs/CASCADE_OPERATIONS.md)
- [`docs/CASCADE_TROUBLESHOOTING.md`](docs/CASCADE_TROUBLESHOOTING.md)

Phase 10 tooling and documentation are implemented. Final acceptance requires a
live second Exit and client-side external-IP verification for both profiles.

## Legal note

Use only for lawful private infrastructure, remote access, security, testing, and administration. Do not use for unlawful access, abuse, spam, or prohibited content distribution.
