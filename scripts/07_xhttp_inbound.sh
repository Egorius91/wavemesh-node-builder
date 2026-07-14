#!/usr/bin/env bash

# shellcheck source=scripts/lib/xui_api.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/xui_api.sh"

wm_xui_json_success() { wm_xui_response_success; }

wm_xui_api_post() {
  wm_xui_request POST "$1" json "$2"
}

wm_xui_api_get() {
  wm_xui_request GET "$1" none
}

wm_build_xhttp_inbound_payload() {
  DOMAIN="$DOMAIN" FINGERPRINT="$FINGERPRINT" WM_CONFIG_JSON="$WM_CONFIG_JSON" XHTTP_LOCAL_PORT="$XHTTP_LOCAL_PORT" XHTTP_PATH="$XHTTP_PATH" NODE_NAME="$NODE_NAME" python3 - <<'PY'
import json
import os

port = int(os.environ['XHTTP_LOCAL_PORT'])
path = os.environ['XHTTP_PATH']
domain = os.environ['DOMAIN']
config = json.load(open(os.environ['WM_CONFIG_JSON'], encoding='utf-8'))
clients = []
for index, item in enumerate(config.get('clients', []), start=1):
  credential = next((value for value in item.get('credentials', []) if value.get('route_id') == 'route-standalone-default'), {})
  client_id = item.get('uuid') or credential.get('uuid')
  if not client_id:
    continue
  clients.append({
    'id': client_id,
    'flow': '',
    'email': credential.get('email') or f"wm.{item.get('id') or index}.route-standalone-default",
    'limitIp': 0,
    'totalGB': 0,
    'expiryTime': 0,
    'enable': bool(item.get('enabled', True) and credential.get('enabled', True)),
    'tgId': 0,
    'subId': item.get('subscription_id', ''),
  })
payload = {
  'up': 0,
  'down': 0,
  'total': 0,
  'remark': os.environ['NODE_NAME'] + '-xhttp',
  'enable': True,
  'expiryTime': 0,
  'listen': '127.0.0.1',
  'port': port,
  'protocol': 'vless',
  'settings': {
    'clients': clients,
    'decryption': 'none',
    'fallbacks': []
  },
  'streamSettings': {
    'network': 'xhttp',
    'security': 'none',
    'xhttpSettings': {
      'path': path,
      'host': domain,
      'mode': 'stream-one'
    },
    'externalProxy': [{
      'dest': domain,
      'port': 443,
      'remark': os.environ['NODE_NAME'],
      'forceTls': 'tls',
      'sni': domain,
      'fingerprint': os.environ['FINGERPRINT']
    }]
  },
  'sniffing': {
    'enabled': True,
    'destOverride': ['http', 'tls', 'quic', 'fakedns'],
    'metadataOnly': False,
    'routeOnly': False
  },
  'allocate': {'strategy': 'always'}
}
print(json.dumps(payload, separators=(',', ':')))
PY
}

wm_find_xhttp_inbound_id() {
  local response_file
  response_file="$(mktemp)"
  wm_xui_api_get /panel/api/inbounds/list > "$response_file" 2>/dev/null || true
  FIRST_CLIENT_UUID="$FIRST_CLIENT_UUID" XHTTP_LOCAL_PORT="$XHTTP_LOCAL_PORT" XHTTP_PATH="$XHTTP_PATH" python3 - "$response_file" <<'PY'
import json
import os
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    sys.exit(1)

items = data.get("obj") if isinstance(data, dict) else None
if not isinstance(items, list):
    sys.exit(1)

want_port = int(os.environ["XHTTP_LOCAL_PORT"])
want_path = os.environ["XHTTP_PATH"]
want_uuid = os.environ["FIRST_CLIENT_UUID"]

def as_obj(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return value if isinstance(value, dict) else {}

for inbound in items:
    settings = as_obj(inbound.get("settings"))
    stream = as_obj(inbound.get("streamSettings"))
    xhttp = as_obj(stream.get("xhttpSettings"))
    clients = settings.get("clients") if isinstance(settings.get("clients"), list) else []
    has_client = any(c.get("id") == want_uuid for c in clients if isinstance(c, dict))
    if (
        inbound.get("protocol") == "vless"
        and int(inbound.get("port", -1)) == want_port
        and inbound.get("listen") == "127.0.0.1"
        and stream.get("network") == "xhttp"
        and stream.get("security") == "none"
        and xhttp.get("path") == want_path
        and has_client
    ):
        print(inbound.get("id") or inbound.get("Id") or "")
        sys.exit(0)

sys.exit(1)
PY
  local rc=$?
  rm -f "$response_file"
  return "$rc"
}

wm_update_config_json_xui_inbound() {
  local inbound_id="$1"
  python3 - <<PY
import json
path = "$WM_CONFIG_JSON"
cfg = json.load(open(path, encoding="utf-8"))
xui = cfg.setdefault("installation", {}).setdefault("xui", {})
xui["inbound"] = {
  "creation_mode": "api",
  "id": int("$inbound_id"),
  "protocol": "vless",
  "transport": "xhttp",
  "listen": "127.0.0.1",
  "port": int("$XHTTP_LOCAL_PORT"),
  "path": "$XHTTP_PATH",
  "xhttp_mode": "stream-one",
  "security": "none"
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
  chmod 600 "$WM_CONFIG_JSON"
  wm_export_config_env_from_json
}

wm_create_xhttp_inbound() {
  wm_info "Creating VLESS + XHTTP inbound via 3X-UI API"
  wm_load_config
  FIRST_CLIENT_UUID="${CLIENT_UUIDS%%,*}"
  [[ -n "$FIRST_CLIENT_UUID" ]] || wm_fail "No canonical client UUID found in config.json"

  if ! wm_xui_login; then
    wm_fail "Could not login to 3X-UI API; stopping before subscription generation"
  fi

  local payload response inbound_id
  payload="$(wm_build_xhttp_inbound_payload)"
  response="$(wm_xui_api_post /panel/api/inbounds/add "$payload" 2>/dev/null || true)"

  if ! printf '%s' "$response" | wm_xui_json_success; then
    wm_warn "3X-UI API inbound creation was not confirmed"
    wm_fail "Could not create VLESS + XHTTP inbound through 3X-UI API"
  fi

  inbound_id="$(wm_find_xhttp_inbound_id || true)"
  [[ -n "$inbound_id" ]] || wm_fail "Created inbound was not visible in 3X-UI API list"

  wm_update_config_json_xui_inbound "$inbound_id"
  wm_success "3X-UI inbound created and verified through API"
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
