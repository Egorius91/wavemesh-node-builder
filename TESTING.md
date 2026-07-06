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

## 3. Run installer

Replace the domain and email:

```bash
sudo bash install.sh --domain test.example.com --email admin@example.com --clients 1
```

## 4. Expected results

After the installer completes:

```text
https://test.example.com/              -> Web Identity website
https://test.example.com/sub/<random>/ -> public subscription URL
```

The subscription must contain VLESS links with:

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

## 6. Inspect 3X-UI API/inbound state

```bash
sudo systemctl status x-ui --no-pager
sudo journalctl -u x-ui -n 100 --no-pager
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
- /root/wavemesh-node-report.txt without secrets if sharing publicly;
- x-ui service status;
- sqlite .tables and .schema output;
- inbounds row after API attempt.
```

Do not share real panel passwords, tokens, or private keys.
