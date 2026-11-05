#!/usr/bin/env bash
set -Eeuo pipefail

if ! command -v python3 >/dev/null 2>&1; then
  python3() { python "$@"; }
  export -f python3
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
wm_success() { :; }
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
token_rows='[]'
wm_xui_login() { return 0; }
wm_xui_discover_capabilities() { printf '{}'; }
wm_export_config_env_from_json() { return 0; }
wm_xui_store_api_state() { printf '%s' "$1" > "$tmp_dir/saved"; }
wm_xui_request_success() {
  case "$1 $2" in
    'GET /panel/api/setting/apiTokens') printf '{"obj":%s}' "$token_rows" ;;
    'GET /panel/api/inbounds/list') [[ "$PANEL_TOKEN" == test-token ]] ;;
    'POST /panel/api/setting/apiTokens/create')
      printf 'create\n' >> "$tmp_dir/creates"
      printf '{"obj":{"token":"test-token"}}'
      ;;
    *) return 1 ;;
  esac
}

wm_xui_bootstrap_api_token
[[ "$PANEL_TOKEN" == test-token && "$(cat "$tmp_dir/saved")" == test-token ]]
wm_xui_bootstrap_api_token
[[ "$(wc -l < "$tmp_dir/creates")" -eq 1 ]]

PANEL_TOKEN=""
token_rows='[{"name":"wavemesh-node-builder"}]'
if wm_xui_bootstrap_api_token; then
  echo "a previous create with no saved bearer was accepted" >&2
  exit 1
fi
[[ "$(wc -l < "$tmp_dir/creates")" -eq 1 ]]

echo "xui api adapter tests: OK"
