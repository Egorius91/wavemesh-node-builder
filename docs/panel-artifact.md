# WaveMesh panel candidate

`Full WaveMesh panel candidate` builds the real Vite frontend before compiling
the Go panel with the disabled-creation extension. It targets Linux amd64 with
static musl linkage. The displayed version is `3.4.2-wavemesh.<builder-sha12>`;
this is a modified GPL-3.0 build, not an official upstream release.

The workflow checks out the exact PR head (or push commit), verifies the pinned
upstream and patch, uses `npm ci` with the upstream lockfile, and records Node,
npm, Go, musl/GCC package versions and Go build metadata. GitHub Actions and Node
are pinned; the Ubuntu compiler packages are resolved at build time and recorded.
This is traceable build provenance, **not byte-for-byte reproducibility** or a
claim that the compiler supply chain is hermetic.

## Contents and verification

The downloadable workflow artifact contains:

- `x-ui-linux-amd64-wavemesh.tar.gz`: the complete v3.4.2 Linux amd64 release,
  replacing only `x-ui/x-ui` with the modified panel and embedded real frontend.
  Xray, mtg, geodata, service files and management script retain their reviewed
  release bytes/modes. There are no floating `latest` asset downloads.
- `source.tar.gz`: full corresponding patched upstream source (including
  frontend source and lockfile), the Builder source and build scripts, GPL
  licenses, and a modification notice. This is captured before npm generation.
- `manifest.json`: full Builder/upstream/patch identity, runtime release digest,
  version, source and candidate digests, complete candidate inventory, frontend
  file hashes, tool versions and workflow run/attempt.

Use a separately trusted Builder checkout at the expected SHA to verify:

```sh
python3 scripts/ci/xui_artifact.py verify --output /path/to/candidate --head FULL_BUILDER_SHA
```

The verifier checks the exact allow-listed release inventory and rejects changed
runtime dependencies, missing/extra/duplicate members, traversal, links and
special files. It reads archives without extracting or executing them. A
manifest is not a signature: establish the GitHub run and expected commit through
the authenticated workflow/artifact channel before trusting its panel digest.

To rebuild, follow `.github/workflows/xui-artifact.yml`: prepare a clean checkout
of the pinned upstream, apply `prepare_xui_disabled_contract.py`, run the
`xui_artifact.py source` command to label/archive sources, build the frontend,
run backend contract tests, build the static binary, then package using the
SHA-pinned release in `third_party/3x-ui/runtime-release.json`.

## Promotion boundary

CI tests backend contracts and executes the binary's version command. It then
extracts the verified packaged candidate into a disposable private network/PID
namespace and starts the real panel and pinned Xray. Only loopback exists; no
external route or destination is available. Random fixture credentials, the
SQLite database, generated client configuration and raw logs are discarded.

The smoke verifies HTTP login/CSRF, anonymous write rejection, a real embedded
JavaScript asset against the frontend manifest, disabled creation and duplicate
rejection, SQLite identity/binding persistence, and real VLESS traffic through
the packaged Xray. A healthy enabled control uses the same inbound as the
disabled client. New connections from the disabled client must fail before and
after panel/Xray restart. Activation and disable use authenticated HTTP and must
preserve the same client/binding identity. A sanitized `runtime-smoke.json`
records result flags and exact source/archive/panel/Xray hashes separately from
the candidate. It does not export raw logs or fixture identities.

This is loopback CI evidence, not public TLS/nginx/subscription/client-app,
long-lived session drain, staging or commercial acceptance. The fixture uses
VLESS over plain TCP only within its isolated namespace; production transport
acceptance remains separate. The fixture's Freedom `finalRules` allows only TCP
to the loopback sentinel port and blocks other destinations, following the
[Xray server-side private-address policy](https://xtls.github.io/en/config/outbounds/freedom.html).
It tests duplicate rejection, not backend CREATE
idempotency or a lost-response recovery contract. No installer is invoked and
no GitHub release is published. The ordinary installer still selects upstream.

The first real HTTP smoke exposed duplicate disabled creation in the inherited
upstream append path: matching email/subId was allowed and another entry could
be appended to an inbound even though `clients` has a unique email. The patch
now checks email/UUID/subId collisions under the existing inbound writer lock
for the disabled endpoint and rejects the request before append. Repeated
inbound IDs are rejected before any write. Backend tests cover concurrent calls
and changed identities; smoke checks both normalized SQLite rows and the
inbound's client array. Legacy `/add` semantics are unchanged. This is conflict
rejection, not a multi-inbound transaction or general lost-response replay API.

After the dependency PRs merge, rebuild at the final Builder merge SHA and verify
the new artifact. Never deploy the old PR-head candidate as a merge-bound build.
Before staging use, implement and prove transactional panel installation,
DB/config backup and compatible rollback, process drain and writer exclusion.
The existing panel service also owns Xray, so a restart can interrupt traffic.
The retained upstream management script/updater is not a supported promotion
path: it could overwrite this extension with an ordinary upstream release.
Fleet replacement/expansion and the complete commercial lifecycle still require
separate runtime proof.
