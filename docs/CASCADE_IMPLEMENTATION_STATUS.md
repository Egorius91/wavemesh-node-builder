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

## Phase 5 - Exit role and join manifests

Status: implemented locally; clean Exit VPS verification remains pending.

Completed:

- added `wavemesh exit peer create`, `list`, and `remove`;
- serialized mutating commands with `/run/lock/wavemesh-node.lock`;
- created loopback-only relay inbounds through the parameterized adapter with API read-back;
- generated one random UUID, path, and local port per Entry/Exit pair;
- rendered all route and relay nginx locations from candidate desired state;
- applied Entry IP allowlists when `--entry-ip` is supplied;
- added nginx candidate validation, backup, reload, and restoration on failure;
- generated checksum-protected join manifests with mode `0600` without printing secrets;
- atomically committed config only after inbound and nginx verification;
- added removal ordering that closes the public location before deleting the relay inbound;
- installed CLI libraries and command modules under `/usr/local/lib/wavemesh`.

Changed files:

- `scripts/05_nginx_ssl.sh`
- `scripts/00_common.sh`
- `bin/wavemesh`
- `scripts/lib/nginx_renderer.py`
- `scripts/lib/nginx_renderer.sh`
- `scripts/lib/exit_peer.py`
- `scripts/commands/exit_peer.sh`
- `tests/fixtures/config-exit-v2.json`
- `tests/unit/test_exit_peer.py`

Test commands:

```bash
python3 tests/unit/test_exit_peer.py
bash -n bin/wavemesh scripts/commands/exit_peer.sh scripts/lib/nginx_renderer.sh
```

Known limitations:

- TLS/public endpoint checks require a real Exit VPS and domain;
- manifest trust is operational and checksum detects corruption only;
- credential rotation remains a controlled multi-command procedure;
- full interrupted-transaction recovery is hardened in Phase 9.

Next phase: Entry `cascade add-exit` and route wiring.

## Phase 6 - Entry add-exit

Status: implemented locally; two-VPS verification remains pending.

Completed:

- added `wavemesh cascade add-exit`, `list`, and `status`;
- validated manifest checksum, kind/version, expiry, Entry constraint, UUID, domain, relay path, transport, and TLS fields;
- added DNS resolution, expected IP matching, private-target rejection, and CA/SNI TLS handshake checks;
- added `--allow-private-target` only as an explicit test override;
- made repeated import of the same manifest a no-op and rejected implicit credential replacement;
- generated a distinct route credential UUID for every client/route pair;
- built a TLS-verified VLESS + XHTTP outbound with `allowInsecure=false`;
- created and read back a dedicated route inbound;
- applied outbound/routing through `testOutbound` and `routeTest`;
- rendered and validated the Entry nginx location;
- restored Xray and removed the route inbound if nginx apply failed;
- committed desired config only after all post-checks;
- added a redacted `--dry-run` plan.

Changed files:

- `scripts/lib/cascade.py`
- `scripts/commands/cascade.sh`
- `bin/wavemesh`
- `tests/fixtures/config-entry-v2.json`
- `tests/unit/test_cascade.py`

Test commands:

```bash
python3 tests/unit/test_cascade.py
bash -n scripts/commands/cascade.sh bin/wavemesh
```

Known limitations:

- real DNS/TLS/API/Xray checks require an Entry and Exit VPS;
- `--force-update` and controlled credential rotation are intentionally not implicit;
- route subscription profiles are generated in Phase 7;
- route removal/enable/disable management is completed in Phase 8.

Next phase: multi-route subscriptions and public profile validation.

## Phase 7 - Multi-route subscriptions

Status: completed, including live public Entry URL verification.

Completed:

- generated one private subscription URL per client with a cryptographically random subscription id;
- upgraded predictable legacy `sub-client-*` ids during the first rebuild;
- generated one profile per enabled client/route/exit combination;
- sorted profiles by route order, display name, and route id;
- used only Entry domain, port 443, Entry route path, and per-route user UUID in public links;
- rejected Exit domains/IPs, relay UUID/path, Entry IP, panel port, and local ports in subscription output;
- rendered exact nginx locations for every client subscription;
- preserved the legacy node subscription path as the first-client compatibility output;
- added candidate file generation, backup, public download comparison, and rollback;
- integrated subscription generation into future `cascade add-exit` transactions;
- added `wavemesh subscription rebuild` for existing imported routes.

Changed files:

- `scripts/lib/subscription_renderer.py`
- `scripts/lib/subscription_renderer.sh`
- `scripts/lib/nginx_renderer.py`
- `scripts/commands/subscription.sh`
- `scripts/commands/cascade.sh`
- `scripts/lib/cascade.py`
- `bin/wavemesh`
- `tests/unit/test_subscription_renderer.py`

Test commands:

```bash
python3 tests/unit/test_subscription_renderer.py
bash -n scripts/lib/subscription_renderer.sh scripts/commands/subscription.sh
```

Known limitations:

- Clash and sing-box output are outside the MVP scope;
- health does not automatically remove profiles after transient failures.

Live result:

- `sudo wavemesh subscription validate`: passed on the Entry VPS on 2026-07-13;
- generated files matched the public HTTPS subscription output byte for byte.

### Phase 7 nginx compatibility fix

- replaced file `alias` under an exact trailing-slash location with the established `root` plus `try_files` pattern;
- retained exact matching and no-store response headers.

Next phase: route lifecycle commands, health, and runtime state.

### Phase 6 compatibility fix - Xray response shape

- accepted both known 3X-UI response forms: `obj` as the Xray JSON string and `obj.xraySetting` as a string/object;
- moved parsing from an environment variable to a temporary file to support large templates;
- added regression tests for direct-string, wrapped-object, and direct-object responses.
- normalized `routeTest.obj` when returned as either an object or a JSON string;
- split Xray apply and route-test failure diagnostics while preserving rollback.
- routed warnings to stderr so API errors are not swallowed by response-file redirection;
- added a secret-free `matched/outboundTag` summary for route-test mismatches.
- ensured the private Xray gRPC API inbound exists on `127.0.0.1:62789` with RoutingService enabled;
- preserved or validated an existing API inbound and rejected port conflicts;
- waited for Xray gRPC readiness after a structural template restart.
- unwrapped nested 3X-UI settings envelopes where `obj.xraySetting` contains another object whose `xraySetting` field holds the actual Xray JSON; this prevents false `missing` drift while `routeTest` is healthy.
- detected both the canonical `xray` process name and 3X-UI packaged names such as `xray-linux-amd64` through `/proc/*/exe`, preventing a healthy live core from being reported as stopped.

## Phase 8 - CLI, health, and runtime state

Status: complete; implementation and live Entry VPS verification passed.

Completed:

- added `wavemesh cascade status`, `health`, and `remove-exit` with mandatory JSON output support for status and health;
- added `wavemesh route list`, `enable`, `disable`, and `remove`;
- added `wavemesh reconcile --dry-run|--apply` with a redacted managed-object plan;
- persisted observed state atomically in `/etc/wavemesh-node/runtime.json` with mode `0600`;
- implemented `unknown`, `healthy`, `unhealthy`, `disabled`, and `misconfigured` states;
- required three consecutive successes or failures before normal health transitions;
- checked 3X-UI, Bearer API, loopback panel binding, Xray, nginx, TLS, managed inbounds, outbounds, routing rules, `testOutbound`, `routeTest`, nginx locations, and subscription profiles;
- kept a single transient failure from deleting or hiding a subscription profile;
- made route lifecycle changes regenerate and publicly validate subscriptions before committing desired state;
- made route and Exit removal verify Xray read-back before deleting route inbounds;
- allowed `reconcile --apply` to repair WaveMesh-managed inbounds, routes, nginx locations, and subscriptions while leaving unmanaged Xray objects intact;
- updated stored inbound IDs when reconciliation recreates a missing managed inbound;
- routed Entry diagnostics through the cascade health checks and added role, route, Exit, and health summaries to the private node report.

Changed files:

- `bin/wavemesh`
- `scripts/commands/cascade.sh`
- `scripts/commands/runtime.sh`
- `scripts/lib/runtime_state.py`
- `scripts/09_report.sh`
- `tests/unit/test_runtime_state.py`
- `README.md`
- `docs/CASCADE_IMPLEMENTATION_STATUS.md`

Test commands:

```bash
python3 tests/unit/test_runtime_state.py
python3 tests/unit/test_cascade.py
python3 tests/unit/test_config_migration.py
python3 tests/unit/test_exit_peer.py
python3 tests/unit/test_inbound_adapter.py
python3 tests/unit/test_subscription_renderer.py
python3 tests/unit/test_xray_response.py
python3 tests/unit/test_xray_template.py
bash tests/unit/test_baseline.sh
bash -n install.sh bin/wavemesh scripts/*.sh scripts/lib/*.sh scripts/commands/*.sh
git diff --check
```

Local results:

- all Python unit tests passed;
- baseline test passed through Git Bash;
- Bash syntax checks passed for installer, CLI, libraries, and command modules;
- Python bytecode compilation and `git diff --check` passed.

Live result (2026-07-13):

- `sudo wavemesh reconcile --apply` restored the managed Xray outbound and routing rule and completed nginx validation;
- three consecutive structural and route probes transitioned `route-de-fra-1` from `unknown` to `healthy`;
- `sudo wavemesh cascade health --json` reported both `node_status: healthy` and route `status: healthy` for `RU -> Germany`;
- live compatibility fixes covered nested 3X-UI Xray settings responses and packaged process names such as `xray-linux-amd64`.

Known limitations:

- automated health scheduling is not installed; health runs on explicit CLI invocation;
- shellcheck and GitHub Actions are not available in the current repository environment.

## Phase 9 - Transaction and rollback hardening

Status: complete and live-verified on the Entry VPS on 2026-07-13.

Completed:

- centralized mutation locking with private PID and operation metadata;
- added atomic `in_progress`, `recovering`, `committed`, `rolled_back`, and `rollback_failed` transaction states;
- captured pre-mutation config, runtime, nginx, subscription, Xray-template, and SQLite online-backup snapshots where applicable;
- installed EXIT, INT, TERM, and HUP handling so ordinary failures and signals trigger one common rollback path;
- blocked new mutations while an incomplete or failed-recovery transaction exists;
- added `wavemesh transaction list [--json]` and explicit `recover --id|--latest` commands;
- restored 3X-UI SQLite, Xray, nginx, subscription files, desired/runtime JSON, and newly written secret manifest output;
- normalized restored public subscription directories to `0755` and files to `0644` so nginx can serve transaction snapshots after rollback;
- required post-rollback JSON validation, service checks, nginx validation, Xray read-back, and public subscription validation;
- changed desired-state commits to same-filesystem atomic JSON replacement;
- retained the newest 20 terminal transactions and the newest 20 files per backup family while never pruning incomplete transactions;
- integrated the shared lifecycle into cascade add/remove, route enable/disable/remove, reconcile apply, Exit peer create/remove, and subscription rebuild.

Changed files:

- `bin/wavemesh`
- `scripts/commands/cascade.sh`
- `scripts/commands/exit_peer.sh`
- `scripts/commands/runtime.sh`
- `scripts/commands/subscription.sh`
- `scripts/lib/nginx_renderer.sh`
- `scripts/lib/subscription_renderer.sh`
- `scripts/lib/transaction_state.py`
- `scripts/lib/transaction.sh`
- `scripts/lib/xray_template.sh`
- `tests/unit/test_transaction_state.py`
- `tests/unit/test_transaction_integration.py`
- `tests/unit/test_transaction.sh`
- `README.md`
- `docs/CASCADE_IMPLEMENTATION_STATUS.md`

Test coverage:

- incomplete transactions block new mutations and terminal transactions cannot be recovered;
- traversal-like transaction IDs are rejected;
- automatic and operator rollback restore JSON, nginx, subscriptions, and SQLite snapshots;
- interrupted transactions survive retention while old terminal transactions and backups are pruned;
- every mutating command path is statically required to acquire the shared lock, begin a transaction, and commit it.

Live verification:

- SIGKILL left transaction `20260713T112031Z-6bd5b0` discoverable as `in_progress`;
- a subsequent subscription mutation was rejected with the explicit recovery command;
- `transaction recover --latest` restored the snapshots and marked the transaction `rolled_back`;
- public subscription validation passed after recovery;
- `cascade health --json` reported both the Entry node and route `route-de-fra-1` as `healthy`.

Next phase: multi-Exit E2E verification and operations/troubleshooting documentation.

## Phase 10 - E2E and operator documentation

Status: complete and live-verified with two Exits on 2026-07-13.

Completed:

- added a deterministic E2E topology with one Entry and two independent Exits;
- exercised manifest creation/validation, sequential Exit import, two managed Xray outbounds and rules, nginx rendering, and one subscription containing two ordered profiles;
- verified that profiles expose only the Entry domain and never Exit domains, IPs, relay paths, or relay UUIDs;
- added `wavemesh cascade verify-e2e [--json]` for redacted live validation of two healthy Exits, expected `routeTest` outbounds, and per-client profile coverage;
- added `docs/CASCADE_OPERATIONS.md` for installation, manifest transfer, import, second Exit, client verification, lifecycle, rotation, backup/restore, health, upgrades, and recovery;
- added `docs/CASCADE_TROUBLESHOOTING.md` for the failure scenarios required by the cascade specification;
- added a GitHub Actions workflow for Python, Bash, adapter, transaction, and two-Exit E2E coverage;
- updated `TESTING.md` and the main README with the Phase 10 verification path.
- made resumed installs recover and verify an existing one-time 3X-UI bearer token;
- made CLI diagnostics role-aware so Exit nodes do not require client subscriptions;
- normalized shell entrypoints to LF and enforced LF checkout through `.gitattributes`;
- changed generated 3X-UI client `tgId` values from empty strings to numeric zero for 3X-UI 3.5 compatibility.

Local acceptance:

- the two-Exit E2E fixture passes with two routes and two ordered profiles for one client;
- the live verifier output contains only route ids, display names, managed outbound tags, counts, and health state;
- Python compilation, Bash syntax, existing unit/integration suites, and diff checks passed before publication.

Live verification:

- installed the second Exit as `de-frankfurt-2` and created its Entry-scoped relay peer and private join manifest;
- imported `route-de-frankfurt-2` transactionally alongside the existing `route-de-fra-1` route;
- three consecutive health observations transitioned the new route and Entry node to `healthy`;
- `wavemesh subscription validate` matched desired state to the public subscription output;
- `wavemesh cascade verify-e2e --json` reported two healthy Exits, two routes, one client, and two profiles;
- a real client received both ordered profiles and observed distinct external Exit addresses for `RU -> Germany` and `RU -> Frankfurt`;
- the Frankfurt profile matched the new Exit address, while neither profile exposed the Entry address.
