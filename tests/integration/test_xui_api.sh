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

echo "xui api adapter tests: OK"
