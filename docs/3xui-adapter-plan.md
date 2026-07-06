# 3X-UI adapter plan

This document defines the next implementation layer for `scripts/07_xhttp_inbound.sh`.

## Why an adapter is needed

3X-UI releases may differ in:

- API endpoint names;
- authentication cookie/token behavior;
- SQLite schema;
- inbound JSON structure;
- subscription settings.

For that reason, WaveMesh must not hardcode a fragile database mutation until the target 3X-UI version is inspected on a real test VPS.

## Adapter order

Use this order:

1. Try official/local 3X-UI API.
2. If API is unavailable, inspect SQLite schema.
3. If schema is supported, create a backup and write inbound data.
4. Restart 3X-UI/Xray.
5. Generate fallback subscription from canonical WaveMesh config.
6. Validate subscription output.

## Required final inbound properties

- protocol: VLESS;
- transport: XHTTP;
- listen address: 127.0.0.1;
- local port: random 10000-30000;
- public port in client links: 443;
- TLS inside inbound: disabled;
- TLS termination: nginx;
- public XHTTP path: random `/api/<token>/`;
- subscription path: random `/sub/<token>/`.

## First test-VPS task

After running installer on a disposable VPS, collect:

```bash
sudo systemctl status x-ui --no-pager
sudo find /etc /usr/local -maxdepth 4 -type f -name '*x-ui*.db'
sudo sqlite3 /path/to/x-ui.db '.tables'
sudo sqlite3 /path/to/x-ui.db '.schema'
```

Then manually create one VLESS + XHTTP inbound in the panel and export:

```bash
sudo sqlite3 /path/to/x-ui.db "select * from inbounds;"
```

Use that result to implement the version-specific adapter.

## Safety rule

Every database mutation must:

1. stop x-ui;
2. backup the database;
3. apply changes;
4. start x-ui;
5. validate service status;
6. validate subscription output.
