#!/usr/bin/env bash

WM_INBOUND_TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/inbound_adapter.py"

wm_inbound_reconcile() {
  local desired_file="$1" actual_file effective_file plan action inbound_id response
  actual_file="$(mktemp)"
  wm_xui_request_success GET /panel/api/inbounds/list none > "$actual_file" || { rm -f "$actual_file"; return 1; }
  effective_file="$(mktemp)"
  python3 "$WM_INBOUND_TOOL" merge-clients --desired "$desired_file" --actual "$actual_file" --output "$effective_file" || { rm -f "$actual_file" "$effective_file"; return 1; }
  plan="$(python3 "$WM_INBOUND_TOOL" plan --desired "$effective_file" --actual "$actual_file")" || { rm -f "$actual_file" "$effective_file"; return 1; }
  rm -f "$actual_file"
  action="$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["action"])')"
  inbound_id="$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id") or "")')"
  case "$action" in
    noop) rm -f "$effective_file"; printf '%s\n' "$inbound_id"; return 0 ;;
    add) response="$(wm_xui_request_success POST /panel/api/inbounds/add json "$(cat "$effective_file")")" || { rm -f "$effective_file"; return 1; } ;;
    update) response="$(wm_xui_request_success POST "/panel/api/inbounds/update/${inbound_id}" json "$(cat "$effective_file")")" || { rm -f "$effective_file"; return 1; } ;;
    *) rm -f "$effective_file"; wm_warn "Unknown inbound reconciliation action"; return 1 ;;
  esac

  actual_file="$(mktemp)"
  wm_xui_request_success GET /panel/api/inbounds/list none > "$actual_file" || { rm -f "$actual_file" "$effective_file"; return 1; }
  plan="$(python3 "$WM_INBOUND_TOOL" plan --desired "$effective_file" --actual "$actual_file")" || { rm -f "$actual_file" "$effective_file"; return 1; }
  rm -f "$actual_file" "$effective_file"
  [[ "$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["action"])')" == "noop" ]] || { wm_warn "Inbound read-back does not match desired state"; return 1; }
  printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id") or "")'
}

wm_inbound_set_enabled() {
  local inbound_id="$1" enabled="$2"
  wm_xui_request_success POST "/panel/api/inbounds/setEnable/${inbound_id}" json "{\"enable\":${enabled}}" >/dev/null
}

wm_inbound_set_remark() {
  local inbound_id="$1" remark="$2" actual desired verified
  actual="$(mktemp)"; desired="$(mktemp)"; verified="$(mktemp)"
  wm_xui_request_success GET /panel/api/inbounds/list none > "$actual" || { rm -f "$actual" "$desired" "$verified"; return 1; }
  python3 "$WM_INBOUND_TOOL" set-remark --actual "$actual" --id "$inbound_id" --remark "$remark" --output "$desired" || { rm -f "$actual" "$desired" "$verified"; return 1; }
  wm_xui_request_success POST "/panel/api/inbounds/update/${inbound_id}" json "$(cat "$desired")" >/dev/null || { rm -f "$actual" "$desired" "$verified"; return 1; }
  wm_xui_request_success GET /panel/api/inbounds/list none > "$verified" || { rm -f "$actual" "$desired" "$verified"; return 1; }
  python3 - "$verified" "$inbound_id" "$remark" <<'PY'
import json,sys
items=json.load(open(sys.argv[1],encoding="utf-8")).get("obj",[])
item=next((x for x in items if str(x.get("id") or x.get("Id"))==sys.argv[2]),None)
if not item or item.get("remark")!=sys.argv[3]: raise SystemExit(1)
PY
  local rc=$?; rm -f "$actual" "$desired" "$verified"; return "$rc"
}

wm_inbound_delete() {
  local inbound_id="$1"
  wm_xui_request_success POST "/panel/api/inbounds/del/${inbound_id}" json '{}' >/dev/null
}
