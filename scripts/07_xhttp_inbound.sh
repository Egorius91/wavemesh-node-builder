#!/usr/bin/env bash

XUI_COOKIE_JAR="/tmp/wavemesh-xui-cookies.txt"

wm_xui_base_url() {
  printf 'http://127.0.0.1:%s%s' "$PANEL_PORT" "$PANEL_PATH"
}

wm_xui_api_url() {
  local path="$1"
  local base
  base="$(wm_xui_base_url)"
  printf '%s%s' "${base%/}" "$path"
}

wm_xui_login() {
  wm_info "Logging in to 3X-UI API"
  local url
  url="$(wm_xui_api_url /login)"
  rm -f "$XUI_COOKIE_JAR"

  local response
  response="$(curl -fsS -c "$XUI_COOKIE_JAR" -H 'Content-Type: application/json' -X POST "$url" -d "{\"username\":\"${PANEL_USERNAME}\",\"password\":\"${PANEL_PASSWORD}\"}" 2>/dev/null || true)"

  if [[ -s "$XUI_COOKIE_JAR" ]] && grep -qiE 'success|true|obj' <<< "$response"; then
    wm_success "3X-UI API login OK"
    return 0
  fi

  wm_warn "3X-UI API login not confirmed at $url"
  return 1
}

wm_xui_api_post() {
  local path="$1"
  local payload="$2"
  local url
  url="$(wm_xui_api_url "$path")"
  curl -fsS -b "$XUI_COOKIE_JAR" -c "$XUI_COOKIE_JAR" -H 'Content-Type: application/json' -X POST "$url" -d "$payload"
}

wm_build_xhttp_inbound_payload() {
  python3 - <<PY
import json
port = int('${XHTTP_LOCAL_PORT}')
path = '${XHTTP_PATH}'
client_id = '${FIRST_CLIENT_UUID}'
email = '${NODE_NAME}-1'
payload = {
  'up': 0,
  'down': 0,
  'total': 0,
  'remark': '${NODE_NAME}-xhttp',
  'enable': True,
  'expiryTime': 0,
  'listen': '127.0.0.1',
  'port': port,
  'protocol': 'vless',
  'settings': json.dumps({
    'clients': [{
      'id': client_id,
      'flow': '',
      'email': email,
      'limitIp': 0,
      'totalGB': 0,
      'expiryTime': 0,
      'enable': True,
      'tgId': '',
      'subId': ''
    }],
    'decryption': 'none',
    'fallbacks': []
  }, separators=(',', ':')),
  'streamSettings': json.dumps({
    'network': 'xhttp',
    'security': 'none',
    'xhttpSettings': {
      'path': path,
      'host': '',
      'mode': 'auto'
    }
  }, separators=(',', ':')),
  'sniffing': json.dumps({
    'enabled': True,
    'destOverride': ['http', 'tls', 'quic', 'fakedns'],
    'metadataOnly': False,
    'routeOnly': False
  }, separators=(',', ':')),
  'allocate': json.dumps({'strategy': 'always'}, separators=(',', ':'))
}
print(json.dumps(payload, separators=(',', ':')))
PY
}

wm_create_xhttp_inbound() {
  wm_info "Creating VLESS + XHTTP inbound via 3X-UI API"

  FIRST_CLIENT_UUID="$(cat /proc/sys/kernel/random/uuid)"

  if ! wm_xui_login; then
    wm_warn "Could not login to 3X-UI API. Keeping fallback subscription only."
    wm_warn "Expected final inbound: listen 127.0.0.1:${XHTTP_LOCAL_PORT}, transport xhttp, path ${XHTTP_PATH}, inbound security none."
    echo "XUI_INBOUND_MODE=\"api_login_failed_fallback_subscription\"" >> "$WM_STATE_DIR/config.env"
    echo "FIRST_CLIENT_UUID=\"${FIRST_CLIENT_UUID}\"" >> "$WM_STATE_DIR/config.env"
    return 0
  fi

  local payload response
  payload="$(wm_build_xhttp_inbound_payload)"
  response="$(wm_xui_api_post /panel/api/inbounds/add "$payload" 2>/dev/null || true)"

  if grep -qiE 'success|true' <<< "$response"; then
    echo "XUI_INBOUND_MODE=\"api\"" >> "$WM_STATE_DIR/config.env"
    echo "FIRST_CLIENT_UUID=\"${FIRST_CLIENT_UUID}\"" >> "$WM_STATE_DIR/config.env"
    wm_success "3X-UI inbound created through API"
  else
    echo "XUI_INBOUND_MODE=\"api_failed_fallback_subscription\"" >> "$WM_STATE_DIR/config.env"
    echo "FIRST_CLIENT_UUID=\"${FIRST_CLIENT_UUID}\"" >> "$WM_STATE_DIR/config.env"
    wm_warn "3X-UI API inbound creation was not confirmed. Response: ${response:-empty}"
    wm_warn "Fallback subscription will still be generated from canonical WaveMesh config."
  fi
}

wm_create_clients() {
  wm_info "Creating ${CLIENT_COUNT} canonical client UUID(s)"
  local uuids=()
  if [[ -n "${FIRST_CLIENT_UUID:-}" ]]; then
    uuids+=("$FIRST_CLIENT_UUID")
  fi
  while (( ${#uuids[@]} < CLIENT_COUNT )); do
    uuids+=("$(cat /proc/sys/kernel/random/uuid)")
  done
  CLIENT_UUIDS="$(IFS=,; echo "${uuids[*]}")"
  wm_config_json_set_clients_from_csv "$CLIENT_UUIDS"
  wm_success "Canonical clients stored in config.json"
}
