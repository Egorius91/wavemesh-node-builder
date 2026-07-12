#!/usr/bin/env bash

WM_INBOUND_TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/inbound_adapter.py"

wm_inbound_reconcile() {
  local desired_file="$1" actual_file plan action inbound_id response
  actual_file="$(mktemp)"
  wm_xui_request_success GET /panel/api/inbounds/list none > "$actual_file" || { rm -f "$actual_file"; return 1; }
  plan="$(python3 "$WM_INBOUND_TOOL" plan --desired "$desired_file" --actual "$actual_file")" || { rm -f "$actual_file"; return 1; }
  rm -f "$actual_file"
  action="$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["action"])')"
  inbound_id="$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id") or "")')"
  case "$action" in
    noop) printf '%s\n' "$inbound_id"; return 0 ;;
    add) response="$(wm_xui_request_success POST /panel/api/inbounds/add json "$(cat "$desired_file")")" || return 1 ;;
    update) response="$(wm_xui_request_success POST "/panel/api/inbounds/update/${inbound_id}" json "$(cat "$desired_file")")" || return 1 ;;
    *) wm_warn "Unknown inbound reconciliation action"; return 1 ;;
  esac

  actual_file="$(mktemp)"
  wm_xui_request_success GET /panel/api/inbounds/list none > "$actual_file" || { rm -f "$actual_file"; return 1; }
  plan="$(python3 "$WM_INBOUND_TOOL" plan --desired "$desired_file" --actual "$actual_file")" || { rm -f "$actual_file"; return 1; }
  rm -f "$actual_file"
  [[ "$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["action"])')" == "noop" ]] || { wm_warn "Inbound read-back does not match desired state"; return 1; }
  printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id") or "")'
}

wm_inbound_set_enabled() {
  local inbound_id="$1" enabled="$2"
  wm_xui_request_success POST "/panel/api/inbounds/setEnable/${inbound_id}" json "{\"enable\":${enabled}}" >/dev/null
}

wm_inbound_delete() {
  local inbound_id="$1"
  wm_xui_request_success POST "/panel/api/inbounds/del/${inbound_id}" json '{}' >/dev/null
}
