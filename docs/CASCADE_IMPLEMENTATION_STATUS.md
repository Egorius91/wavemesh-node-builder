# Cascade VPN implementation status

This file tracks implementation against `CASCADE_VPN_SPEC_FOR_CODEX.md`.

## Phase 0 - Audit and baseline

Status: completed locally.

Completed:

- audited the current `main` implementation at `89b45c0`;
- preserved the existing standalone installation flow;
- fixed the duplicate inbound `mode` key by separating `creation_mode` and `xhttp_mode`;
- inventoried current 3X-UI calls;
- added a baseline syntax and duplicate-key test.

Changed files:

- `scripts/07_xhttp_inbound.sh`
- `tests/unit/test_baseline.sh`
- `docs/CASCADE_IMPLEMENTATION_STATUS.md`

Test commands:

```bash
bash tests/unit/test_baseline.sh
```

Known limitations:

- a full standalone E2E test requires a clean Ubuntu 24.04 VPS, a domain, and Let's Encrypt access;
- current API authentication still uses a cookie and CSRF token;
- `PANEL_TOKEN` is not yet a real 3X-UI API token.

### Current 3X-UI API inventory

| Operation | Endpoint | Authentication | Caller |
|---|---|---|---|
| CSRF bootstrap | `GET /csrf-token` | none/session bootstrap | `wm_xui_get_csrf` |
| Login | `POST /login` | cookie + CSRF | `wm_xui_login` |
| List inbounds | `GET /panel/api/inbounds/list` | cookie | `wm_find_xhttp_inbound_id` |
| Add inbound | `POST /panel/api/inbounds/add` | cookie + CSRF | `wm_create_xhttp_inbound` |

Next phase: config schema v2, migration, atomic JSON helpers, and role-aware install inputs.

## Phase 1 - Config schema v2 and migration

Status: implemented locally; clean-VPS regression remains pending.

Completed:

- added `--role`, `--node-id`, `--country`, and `--city` inputs;
- made `standalone` the default role;
- added config v2 and exit join manifest JSON Schemas;
- added an automatic, idempotent v1 to v2 migration with a timestamped backup;
- preserved the legacy direct route, UUID, subscription path, and installation data;
- stopped generating a random value that pretended to be a 3X-UI bearer token;
- retained `config.env` as a compatibility export generated from `config.json`;
- ignored secret join manifests in Git.

Changed files:

- `scripts/00_common.sh`
- `scripts/lib/config_json.py`
- `schemas/config-v2.schema.json`
- `schemas/exit-join-v1.schema.json`
- `tests/fixtures/config-v1.json`
- `tests/unit/test_config_migration.py`
- `.gitignore`

Test commands:

```bash
python3 tests/unit/test_config_migration.py
bash tests/unit/test_baseline.sh
```

Local results:

- migration data and idempotency test: passed on Windows;
- both schema files parse as valid JSON;
- `git diff --check`: passed;
- Bash syntax test: passed for `install.sh`, `bin/wavemesh`, and all top-level scripts using Git Bash.

Known limitations:

- role-aware data-plane installation is not complete yet;
- bearer token bootstrap and capability discovery belong to Phase 2;
- POSIX `0600` behavior is asserted only when tests run on a POSIX host.

Next phase: common 3X-UI API client and capability discovery.

## Phase 2 - Common 3X-UI API client

Status: implemented locally; live-panel verification remains pending.

Completed:

- added a shared API client with separate transport, HTTP status, JSON, and `success` checks;
- added bounded connect/request timeouts and contextual errors without response-secret logging;
- retained cookie and CSRF only for bootstrap and compatibility fallback;
- added OpenAPI capability discovery for every endpoint required by the cascade data plane;
- added creation and immediate verification of a real 3X-UI Bearer token;
- stopped treating a locally generated random string as an API token;
- stored the one-time token with the private config and exported it only to root-owned compatibility state;
- refactored the standalone inbound flow to use the shared API client.

Changed files:

- `scripts/lib/xui_api.sh`
- `scripts/07_xhttp_inbound.sh`
- `install.sh`
- `tests/fixtures/xui-openapi.json`
- `tests/integration/test_xui_api.sh`

Test commands:

```bash
bash tests/integration/test_xui_api.sh
bash -n install.sh scripts/07_xhttp_inbound.sh scripts/lib/xui_api.sh
```

Known limitations:

- token bootstrap must still be verified against the exact latest release selected on a clean VPS;
- WebSocket APIs intentionally remain cookie-only and are not used by WaveMesh;
- Xray template mutation belongs to Phase 3.

Next phase: transactional Xray template adapter.

## Phase 3 - Transactional Xray template adapter

Status: implemented locally; live 3X-UI/Xray verification remains pending.

Completed:

- added deterministic merge and removal of WaveMesh-managed outbounds and routing rules;
- preserved all unmanaged user outbounds, rules, and top-level template fields;
- inserted managed route rules before the first catch-all rule;
- rejected duplicate outbound and rule tags;
- added idempotent replacement for an existing managed route;
- added API helpers for template read/update, `testOutbound`, and `routeTest`;
- added transaction directories and timestamped template backups;
- added automatic restoration of the original template when apply or post-check fails;
- kept TLS certificate validation enabled in the VLESS outbound fixture.

Changed files:

- `scripts/lib/xray_template.py`
- `scripts/lib/xray_template.sh`
- `tests/fixtures/xray-template.json`
- `tests/fixtures/xray-outbound-de.json`
- `tests/unit/test_xray_template.py`

Test commands:

```bash
python3 tests/unit/test_xray_template.py
bash -n scripts/lib/xray_template.sh
```

Known limitations:

- no cascade command invokes the adapter until the Exit and Entry flows are implemented;
- interruption recovery and transaction locking are hardened in Phase 9;
- live `testOutbound`/`routeTest` response shapes must be confirmed on the selected release.

Next phase: parameterized inbound adapter with read-back and idempotency.

## Phase 4 - Parameterized inbound adapter

Status: implemented locally; integration into role commands remains pending.

Completed:

- added one XHTTP inbound builder usable for standalone, route, and relay purposes;
- enforced loopback listen, `stream-one`, explicit managed tags, and structured client input;
- added add/update/no-op planning based on actual API state;
- rejected ambiguous duplicate managed tags;
- added API read-back after every add or update;
- added focused enable, disable, and delete operations.

Changed files:

- `scripts/lib/inbound_adapter.py`
- `scripts/lib/inbound_adapter.sh`
- `tests/fixtures/inbound-clients.json`
- `tests/unit/test_inbound_adapter.py`

Test commands:

```bash
python3 tests/unit/test_inbound_adapter.py
bash -n scripts/lib/inbound_adapter.sh
```

Known limitations:

- the legacy standalone builder is retained until route-aware install integration;
- Exit peer creation is implemented in Phase 5;
- actual inbound tags must be verified against the live API read-back on the test VPS.

Next phase: Exit role, relay peers, allowlist, and join manifests.
