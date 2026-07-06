# WaveMesh Node Builder

Bash-based installer/wizard for quickly deploying a WaveMesh node on a clean Ubuntu/Debian VPS.

MVP goals:

- create a Web Identity corporate website;
- configure nginx and Let's Encrypt SSL;
- install 3X-UI;
- create VLESS + XHTTP topology through nginx;
- generate a correct public subscription URL;
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
https://domain.com/sub/<random>/
```

Every VLESS link inside the subscription must use:

```text
domain.com:443 + security=tls + type=xhttp + correct xhttp path
```

## Quick start

```bash
sudo bash install.sh --domain example.com --email admin@example.com
```

Interactive wizard is also supported:

```bash
sudo bash install.sh
```

## Files created on a node

```text
/etc/wavemesh-node/config.env
/etc/wavemesh-node/report.json
/root/wavemesh-node-report.txt
/root/wavemesh-node-report.json
/var/www/wavemesh-site
/var/www/wavemesh-sub
```

## CLI

After installation:

```bash
wavemesh show-report
wavemesh diagnostics
wavemesh validate-subscription
wavemesh repair --nginx
wavemesh repair --ssl
wavemesh repair --subscriptions
```

## Current MVP status

The first skeleton implements argument parsing, root/system checks, random paths and ports, firewall baseline, BBR enablement, Web Identity generation, nginx/SSL templates, fallback-generated subscription files, strict validation, report generation, and a CLI wrapper.

3X-UI API integration and real inbound creation are intentionally isolated in dedicated scripts for the next iteration.

## Legal note

Use only for lawful private infrastructure, remote access, security, testing, and administration. Do not use for unlawful access, abuse, spam, or prohibited content distribution.
