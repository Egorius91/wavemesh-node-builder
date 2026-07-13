#!/usr/bin/env bash
set -Eeuo pipefail

if ! command -v python3 >/dev/null 2>&1; then
  python3() { python "$@"; }
  export -f python3
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
wm_warn() { printf '%s\n' "$*" >&2; }
PANEL_PORT=50000
PANEL_PATH=/panel-test/
PANEL_TOKEN=""

# shellcheck source=scripts/lib/xui_api.sh
source "$ROOT_DIR/scripts/lib/xui_api.sh"

printf '{"success":true}' | wm_xui_response_success
if printf '{"success":false,"msg":"expected"}' | wm_xui_response_success; then
  echo "success=false was accepted" >&2
  exit 1
fi

wm_xui_request() {
  cat "$ROOT_DIR/tests/fixtures/xui-openapi.json"
}
capabilities="$(wm_xui_discover_capabilities)"
CAPABILITIES="$capabilities" python3 - <<'PY'
import json, os
data=json.loads(os.environ["CAPABILITIES"])
assert data and all(data.values())
PY

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
XUI_RUNTIME_BIN="$tmp_dir/x-ui"
cat > "$XUI_RUNTIME_BIN" <<'SH'
#!/usr/bin/env bash
printf 'apiToken: recovered-test-token\n'
SH
chmod +x "$XUI_RUNTIME_BIN"

recovered_token="$(wm_xui_read_existing_api_token)"
[[ "$recovered_token" == "recovered-test-token" ]]

echo "xui api adapter tests: OK"
