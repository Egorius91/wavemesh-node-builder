#!/usr/bin/env bash

WM_XRAY_TEMPLATE_TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/xray_template.py"

wm_form_field() {
  local name="$1" value="$2"
  NAME="$name" VALUE="$value" python3 -c 'import os,urllib.parse; print(urllib.parse.urlencode({os.environ["NAME"]:os.environ["VALUE"]}))'
}

wm_xray_get_template() {
  local response output="$1"
  response="$(wm_xui_request_success POST /panel/api/xray/ json '{}')" || return 1
  RESPONSE="$response" OUTPUT="$output" python3 - <<'PY'
import json, os
data=json.loads(os.environ["RESPONSE"])
raw=(data.get("obj") or {}).get("xraySetting")
if isinstance(raw, str): raw=json.loads(raw)
if not isinstance(raw, dict): raise SystemExit("3X-UI xraySetting is not an object")
with open(os.environ["OUTPUT"],"w",encoding="utf-8") as out:
    json.dump(raw,out,indent=2,sort_keys=True); out.write("\n")
PY
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
  local inbound_tag="$1" expected_outbound="$2" payload response actual
  payload="domain=example.com&port=443&network=tcp&$(wm_form_field inboundTag "$inbound_tag")"
  response="$(wm_xui_request_success POST /panel/api/xray/routeTest form "$payload")" || return 1
  actual="$(printf '%s' "$response" | python3 -c 'import json,sys; obj=json.load(sys.stdin).get("obj"); print(obj if isinstance(obj,str) else (obj or {}).get("outboundTag", ""))' 2>/dev/null || true)"
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
  if ! wm_xray_apply_template "$candidate" || ! wm_xray_route_test "$inbound_tag" "$outbound_tag"; then
    wm_warn "Xray candidate failed post-check; restoring previous template"
    wm_xray_apply_template "$original" || wm_warn "Automatic Xray rollback failed; restore $original manually"
    return 1
  fi
  printf '%s\n' '{"status":"committed"}' > "$transaction_dir/status.json"
  chmod 600 "$transaction_dir"/*.json
}
