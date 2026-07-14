# 3X-UI API mode

> Historical design note: the generated subscription backend described below
> is retained only for rollback. New nodes use the native backend documented in
> `NATIVE_SUBSCRIPTIONS.md`.

WaveMesh Node Builder uses 3X-UI API as the primary write method.

3X-UI officially provides a RESTful API with in-panel Swagger documentation, and the installer supports unattended installation with `XUI_NONINTERACTIVE=1` and writes install results to `/etc/x-ui/install-result.env`.

## Adapter flow

1. Install 3X-UI.
2. Configure panel credentials, port and web base path.
3. Login locally:

```text
POST http://127.0.0.1:<panel_port>/<panel_path>/login
```

4. Create inbound:

```text
POST http://127.0.0.1:<panel_port>/<panel_path>/panel/api/inbounds/add
```

5. Use nginx as the only public TLS listener.
6. Generate canonical WaveMesh subscription from config.
7. Validate public subscription.

## Required inbound

```text
protocol: vless
transport: xhttp
listen: 127.0.0.1
port: random local port
security inside inbound: none
TLS: terminated by nginx
public link: domain.com:443
```

## Why fallback subscription remains

Even if inbound creation succeeds, WaveMesh keeps its own generated subscription as the canonical public output. This prevents accidental leakage of:

- 127.0.0.1;
- localhost;
- raw VPS IP;
- local XHTTP port;
- panel port;
- `security=none` in public client links.

## Test command after install

```bash
wavemesh validate-subscription
```

## Manual API inspection

Open the panel and check the in-panel Swagger documentation for the exact schema of the installed 3X-UI version. If the `/panel/api/inbounds/add` payload changes, update only `scripts/07_xhttp_inbound.sh`.
