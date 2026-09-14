# Disabled client creation contract

This directory contains a WaveMesh modification of 3X-UI 3.4.2, based on upstream
commit `f3a57d4c57fbcae94414138de42b7ef11dc513c8`. The upstream source and this patch
are GPL-3.0; see `LICENSE`. Changes made on 2026-09-14 are identified by this patch
and the WaveMesh extension in the API reference. This is **source and CI contract
coverage, not an installed or release-ready panel artifact**.

The installed staging binary was matched to the upstream release archive. Its
`clients/add` path forces `enable=false` to `true`, even for an expired client.
The client-record INSERT also has a GORM `default:true` that rewrites false.
The previous Node fixture hid both behaviors. Creating enabled and disabling in
a second request cannot prove that replacement credentials were never usable.

## Contract

The patch adds authenticated `POST /panel/api/clients/addDisabled`, accepting
the existing create payload with `client.enable=false` and only local VLESS
inbounds. All requested targets are checked before the first write. The existing
`/add` default remains compatible. Explicit false is preserved in client-record
INSERTs within a transaction, including rollback when that correction fails.

The Node Agent selects the new endpoint for `access.prepare_replacement`. An
unpatched panel rejects it; there is **no fallback** to `/add`. Enabled readback
is a contract violation, not something to silently compensate and accept. The
existing durable transport barrier still retains uncertain outcomes.

CI fetches the exact upstream commit, verifies this patch's SHA-256, applies it,
and runs the real service/controller suites plus race-checked contract tests.
SQLite and the actual local-runtime dispatch code are exercised with a counting
API-port callback; no real Xray process or VPN traffic is involved. The tests
prove false in client rows and inbound settings, no runtime AddUser attempt,
authenticated route access, early target rejection, and rollback on persistence
failure. The unchanged legacy route is characterized as enabled by default.

To prepare an isolated clean source checkout:

```sh
python3 scripts/ci/prepare_xui_disabled_contract.py /absolute/3x-ui-source
```

No installer, service restart, credential migration, or release promotion is
performed by that command or CI. Never apply the patch to a running installation.

## Deployment prerequisites still open

Build a complete panel artifact (including its real frontend), label it as a
WaveMesh-modified build, bind it to both upstream and Builder merge SHAs, and
prove transactional panel deployment/rollback. Install and accept that artifact
before enabling replacement preparation. The normal installer still selects an
upstream release; it does not yet install this extension.

This contract does not make a multi-inbound request atomic or idempotent at the
backend. It does not exclude panel UI/remote writers or prove request drain.
Partial disabled creation and ambiguous requests must retain the original
identity and remain subject to reconciliation. Do not reset the journal or retry
with new credentials. Concurrent topology changes and fleet quarantine still
require the exclusive-writer/quiescence work tracked by SaaS #150 and draft #273.
