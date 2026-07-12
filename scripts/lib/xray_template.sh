#!/usr/bin/env bash

WM_XRAY_TEMPLATE_TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/xray_template.py"
WM_XRAY_RESPONSE_TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/xray_response.py"

wm_form_field() {
  local name="$1" value="$2"
  NAME="$name" VALUE="$value" python3 -c 'import os,urllib.parse; print(urllib.parse.urlencode({os.environ["NAME"]:os.environ["VALUE"]}))'
}

wm_xray_get_template() {
  local output="$1" response_file
  response_file="$(mktemp)"
  if ! wm_xui_request_success POST /panel/api/xray/ json '{}' > "$response_file"; then
    rm -f "$response_file"
    return 1
  fi
  python3 "$WM_XRAY_RESPONSE_TOOL" --response "$response_file" --output "$output"
  local rc=$?
  rm -f "$response_file"
  return "$rc"
}

wm_xray_test_outbound() {
  local outbound_file="$1" all_file="$2" outbound all payload
  outbound="$(cat "$outbound_file")"; all="$(python3 - "$all_file" <<'PY'
import json,sys
print(json.dumps(json.load(open(sys.argv[1],encoding="utf-8")).get("outbounds",[]),separators=(",",":")))
PY
)"
  payload="$(wm_form_field outbound "$outbound")&$(wm_form_field allOutbounds "$all")&mode=tcp"
  wm_xui_request_success POST /panel/api/xray/testOutbound form "$payload" >/dev/null
}

wm_xray_apply_template() {
  local template_file="$1" template payload
  template="$(cat "$template_file")"
  payload="$(wm_form_field xraySetting "$template")&$(wm_form_field outboundTestUrl 'https://www.google.com/generate_204')"
  wm_xui_request_success POST /panel/api/xray/update form "$payload" >/dev/null
}

wm_xray_route_test() {
  local inbound_tag="$1" expected_outbound="$2" payload response_file actual
  payload="domain=example.com&port=443&network=tcp&$(wm_form_field inboundTag "$inbound_tag")"
  response_file="$(mktemp)"
  if ! wm_xui_request_success POST /panel/api/xray/routeTest form "$payload" > "$response_file"; then rm -f "$response_file"; return 1; fi
  actual="$(python3 -c 'import importlib.util,json,sys; spec=importlib.util.spec_from_file_location("xr",sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(m.extract_route_outbound(json.load(open(sys.argv[2],encoding="utf-8"))))' "$WM_XRAY_RESPONSE_TOOL" "$response_file" 2>/dev/null || true)"
  rm -f "$response_file"
  [[ "$actual" == "$expected_outbound" ]] || { wm_warn "routeTest selected ${actual:-no outbound}, expected ${expected_outbound}"; return 1; }
}

wm_xray_apply_managed_route() {
  local outbound_file="$1" inbound_tag="$2" outbound_tag="$3" rule_tag="$4"
  local transaction_dir original candidate
  transaction_dir="$WM_STATE_DIR/transactions/$(date -u +%Y%m%dT%H%M%SZ)-xray-$$"
  mkdir -p "$transaction_dir" "$WM_STATE_DIR/backups"
  chmod 700 "$WM_STATE_DIR/transactions" "$transaction_dir"
  original="$transaction_dir/original.json"; candidate="$transaction_dir/candidate.json"

  wm_xray_get_template "$original" || return 1
  cp "$original" "$WM_STATE_DIR/backups/xray.$(date -u +%Y%m%dT%H%M%SZ).json"
  python3 "$WM_XRAY_TEMPLATE_TOOL" merge --template "$original" --outbound "$outbound_file" --inbound-tag "$inbound_tag" --outbound-tag "$outbound_tag" --rule-tag "$rule_tag" --output "$candidate" || return 1
  wm_xray_test_outbound "$outbound_file" "$candidate" || return 1
  if ! wm_xray_apply_template "$candidate"; then
    wm_warn "Xray candidate apply failed; restoring previous template"
    wm_xray_apply_template "$original" || wm_warn "Automatic Xray rollback failed; restore $original manually"
    return 1
  fi
  if ! wm_xray_route_test "$inbound_tag" "$outbound_tag"; then
    wm_warn "Xray routeTest failed; restoring previous template"
    wm_xray_apply_template "$original" || wm_warn "Automatic Xray rollback failed; restore $original manually"
    return 1
  fi
  printf '%s\n' '{"status":"committed"}' > "$transaction_dir/status.json"
  chmod 600 "$transaction_dir"/*.json
}
