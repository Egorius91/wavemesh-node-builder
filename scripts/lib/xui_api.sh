#!/usr/bin/env bash

XUI_COOKIE_JAR="${XUI_COOKIE_JAR:-/tmp/wavemesh-xui-cookies.txt}"
XUI_CSRF_TOKEN="${XUI_CSRF_TOKEN:-}"
XUI_API_TIMEOUT="${XUI_API_TIMEOUT:-15}"

wm_xui_base_url() {
  printf 'http://127.0.0.1:%s%s' "$PANEL_PORT" "$PANEL_PATH"
}

wm_xui_api_url() {
  printf '%s%s' "$(wm_xui_base_url | sed 's#/$##')" "$1"
}

wm_xui_response_success() {
  python3 -c 'import json,sys; data=json.load(sys.stdin); sys.exit(0 if data.get("success") is True else 1)' 2>/dev/null
}

wm_xui_response_message() {
  python3 -c 'import json,sys; data=json.load(sys.stdin); print(data.get("msg") or "3X-UI returned success=false")' 2>/dev/null || printf 'invalid JSON response'
}

wm_xui_get_csrf() {
  local response
  response="$(curl -fsS -c "$XUI_COOKIE_JAR" -b "$XUI_COOKIE_JAR" --connect-timeout 5 --max-time "$XUI_API_TIMEOUT" "$(wm_xui_api_url /csrf-token)" 2>/dev/null || true)"
  XUI_CSRF_TOKEN="$(printf '%s' "$response" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("obj", ""))' 2>/dev/null || true)"
  [[ -n "$XUI_CSRF_TOKEN" ]]
}

wm_xui_login() {
  local payload response
  rm -f "$XUI_COOKIE_JAR"
  wm_xui_get_csrf || return 1
  payload="$(PANEL_USERNAME="$PANEL_USERNAME" PANEL_PASSWORD="$PANEL_PASSWORD" python3 - <<'PY'
import json, os
print(json.dumps({"username": os.environ["PANEL_USERNAME"], "password": os.environ["PANEL_PASSWORD"]}))
PY
)"
  response="$(curl -fsS -c "$XUI_COOKIE_JAR" -b "$XUI_COOKIE_JAR" --connect-timeout 5 --max-time "$XUI_API_TIMEOUT" -H 'Content-Type: application/json' -H "X-CSRF-Token: ${XUI_CSRF_TOKEN}" -X POST "$(wm_xui_api_url /login)" --data-binary "$payload" 2>/dev/null || true)"
  [[ -s "$XUI_COOKIE_JAR" ]] && printf '%s' "$response" | wm_xui_response_success
}

wm_xui_request() {
  local method="$1" path="$2" content_type="${3:-json}" payload="${4:-}" url body_file status auth_mode
  url="$(wm_xui_api_url "$path")"
  body_file="$(mktemp)"
  auth_mode="cookie"
  local args=(--silent --show-error --connect-timeout 5 --max-time "$XUI_API_TIMEOUT" --output "$body_file" --write-out '%{http_code}' --request "$method")

  if [[ -n "${PANEL_TOKEN:-}" ]]; then
    args+=(-H "Authorization: Bearer ${PANEL_TOKEN}")
    auth_mode="bearer"
  else
    [[ -s "$XUI_COOKIE_JAR" ]] || wm_xui_login || { rm -f "$body_file"; return 1; }
    args+=(-b "$XUI_COOKIE_JAR" -c "$XUI_COOKIE_JAR")
    if [[ "$method" != "GET" && "$method" != "HEAD" ]]; then
      [[ -n "$XUI_CSRF_TOKEN" ]] || wm_xui_get_csrf || { rm -f "$body_file"; return 1; }
      args+=(-H "X-CSRF-Token: ${XUI_CSRF_TOKEN}")
    fi
  fi

  case "$content_type" in
    json) args+=(-H 'Content-Type: application/json'); [[ -n "$payload" ]] && args+=(--data-binary "$payload") ;;
    form) args+=(-H 'Content-Type: application/x-www-form-urlencoded'); [[ -n "$payload" ]] && args+=(--data-binary "$payload") ;;
    none) ;;
    *) rm -f "$body_file"; wm_warn "Unsupported 3X-UI request content type"; return 1 ;;
  esac

  status="$(curl "${args[@]}" "$url" 2>/dev/null || true)"
  if [[ ! "$status" =~ ^2[0-9][0-9]$ ]]; then
    wm_warn "3X-UI ${method} ${path} failed with HTTP ${status:-transport-error} (${auth_mode} auth)"
    rm -f "$body_file"
    return 1
  fi
  cat "$body_file"
  rm -f "$body_file"
}

wm_xui_request_success() {
  local response
  response="$(wm_xui_request "$@")" || return 1
  if ! printf '%s' "$response" | wm_xui_response_success; then
    wm_warn "3X-UI operation failed: $(printf '%s' "$response" | wm_xui_response_message)"
    return 1
  fi
  printf '%s' "$response"
}

wm_xui_wait_ready() {
  local attempt attempts="${WM_XUI_READY_ATTEMPTS:-30}"
  for (( attempt=1; attempt<=attempts; attempt++ )); do
    if systemctl is-active --quiet x-ui && wm_xui_request_success GET /panel/api/inbounds/list none >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  wm_warn "3X-UI API did not become ready after ${attempts} seconds"
  return 1
}

wm_xui_discover_capabilities() {
  local openapi_file
  openapi_file="$(mktemp)"
  wm_xui_request GET /panel/api/openapi.json none > "$openapi_file" || { rm -f "$openapi_file"; return 1; }
  python3 - "$openapi_file" <<'PY'
import json, sys
data=json.load(open(sys.argv[1], encoding="utf-8"))
paths=data.get("paths", {})
required={
  "inbound_list": "/panel/api/inbounds/list",
  "inbound_add": "/panel/api/inbounds/add",
  "api_token_create": "/panel/api/setting/apiTokens/create",
  "xray_template": "/panel/api/xray/",
  "xray_update": "/panel/api/xray/update",
  "test_outbound": "/panel/api/xray/testOutbound",
  "route_test": "/panel/api/xray/routeTest",
}
result={name: path in paths for name,path in required.items()}
print(json.dumps(result, separators=(",", ":"), sort_keys=True))
if not all(result.values()):
    sys.exit(2)
PY
  local rc=$?
  rm -f "$openapi_file"
  return "$rc"
}

wm_xui_store_api_state() {
  local token="$1" token_name="$2" capabilities="$3"
  WM_CONFIG_JSON="$WM_CONFIG_JSON" TOKEN="$token" TOKEN_NAME="$token_name" CAPABILITIES="$capabilities" python3 - <<'PY'
import json, os, tempfile
from pathlib import Path

path=Path(os.environ["WM_CONFIG_JSON"])
cfg=json.loads(path.read_text(encoding="utf-8"))
cfg.setdefault("panel", {})["api_auth"]={"mode":"bearer","token_name":os.environ["TOKEN_NAME"],"token":os.environ["TOKEN"]}
cfg.setdefault("installation", {}).setdefault("xui", {})["adapter"]={"version":1,"capabilities":json.loads(os.environ["CAPABILITIES"])}
fd,temp=tempfile.mkstemp(prefix=".config.", dir=path.parent)
try:
    with os.fdopen(fd,"w",encoding="utf-8") as out:
        json.dump(cfg,out,indent=2,ensure_ascii=False,sort_keys=True); out.write("\n"); out.flush(); os.fsync(out.fileno())
    os.chmod(temp,0o600); os.replace(temp,path)
finally:
    if os.path.exists(temp): os.unlink(temp)
PY
  chmod 600 "$WM_CONFIG_JSON"
}

wm_xui_read_existing_api_token() {
  local output token
  [[ -n "${XUI_RUNTIME_BIN:-}" && -x "$XUI_RUNTIME_BIN" ]] || return 1

  output="$("$XUI_RUNTIME_BIN" setting -getApiToken true 2>/dev/null || \
    "$XUI_RUNTIME_BIN" setting -getApiToken 2>/dev/null || true)"
  token="$(printf '%s\n' "$output" | sed -n 's/^[[:space:]]*apiToken:[[:space:]]*//p' | head -n 1)"
  [[ -n "$token" ]] || return 1
  printf '%s' "$token"
}

wm_xui_bootstrap_api_token() {
  local capabilities token_name payload response token existing_token
  if [[ -n "${PANEL_TOKEN:-}" ]]; then
    wm_xui_request_success GET /panel/api/inbounds/list none >/dev/null || return 1
    return 0
  fi

  wm_xui_login || { wm_warn "Could not bootstrap 3X-UI cookie session"; return 1; }
  capabilities="$(wm_xui_discover_capabilities)" || { wm_warn "Installed 3X-UI lacks required cascade API capabilities"; return 1; }
  token_name="wavemesh-${NODE_ID:-node-builder}"
  token_name="${token_name:0:64}"

  existing_token="$(wm_xui_read_existing_api_token || true)"
  if [[ -n "$existing_token" ]]; then
    PANEL_TOKEN="$existing_token"
    if wm_xui_request_success GET /panel/api/inbounds/list none >/dev/null; then
      wm_xui_store_api_state "$existing_token" "$token_name" "$capabilities"
      wm_export_config_env_from_json
      wm_success "Existing 3X-UI bearer API token recovered and verified"
      return 0
    fi
    PANEL_TOKEN=""
    wm_warn "Existing 3X-UI bearer token failed verification; creating a replacement"
  fi

  payload="$(TOKEN_NAME="$token_name" python3 -c 'import json,os; print(json.dumps({"name":os.environ["TOKEN_NAME"]}))')"
  response="$(wm_xui_request_success POST /panel/api/setting/apiTokens/create json "$payload")" || return 1
  token="$(printf '%s' "$response" | python3 -c 'import json,sys; obj=json.load(sys.stdin).get("obj") or {}; print(obj.get("token") or obj.get("value") or "")' 2>/dev/null || true)"
  [[ -n "$token" ]] || { wm_warn "3X-UI did not return the one-time API token value"; return 1; }

  wm_xui_store_api_state "$token" "$token_name" "$capabilities"
  PANEL_TOKEN="$token"
  wm_xui_request_success GET /panel/api/inbounds/list none >/dev/null || { wm_warn "New 3X-UI bearer token failed verification"; return 1; }
  wm_export_config_env_from_json
  wm_success "3X-UI bearer API token created and verified"
}
