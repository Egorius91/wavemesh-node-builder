#!/usr/bin/env bash

WM_E2E_CHECK_TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/e2e_check.py"

wm_require_entry_role() { [[ "$NODE_ROLE" == "entry" ]] || wm_fail "This command requires an entry node"; }

wm_cascade_verify_e2e() {
  local output_json=0
  while [[ $# -gt 0 ]]; do case "$1" in --json) output_json=1; shift;; *) wm_fail "Usage: wavemesh cascade verify-e2e [--json]";; esac; done
  wm_load_config; wm_require_entry_role
  if [[ "$(wm_subscription_backend "$WM_CONFIG_JSON")" == "xui-native" ]]; then
    local health
    health="$(mktemp)"
    wm_runtime_health --json > "$health" || { rm -f "$health"; wm_fail "Managed route health verification failed"; }
    python3 - "$health" <<'PY' || { rm -f "$health"; wm_fail "One or more enabled routes are unhealthy"; }
import json,sys
data=json.load(open(sys.argv[1],encoding="utf-8"))
bad=[item for item in data.get("routes",[]) if item.get("status") not in ("healthy","disabled")]
if bad: raise SystemExit(1)
PY
    wm_native_validate_public "$WM_CONFIG_JSON" || { rm -f "$health"; wm_fail "Native subscription E2E validation failed"; }
    if (( output_json == 1 )); then
      HEALTH="$health" python3 - <<'PY'
import json,os
data=json.load(open(os.environ["HEALTH"],encoding="utf-8")); print(json.dumps({"backend":"xui-native","status":"ok","routes":data.get("routes",[])},indent=2,ensure_ascii=False))
PY
    else
      wm_success "Native subscription and managed route E2E checks passed"
    fi
    rm -f "$health"
    return
  fi
  local args=(--config "$WM_CONFIG_JSON" --runtime "$WM_RUNTIME_JSON" --subscriptions "$WM_SUB_DIR")
  if (( output_json == 1 )); then args+=(--json); fi
  python3 "$WM_E2E_CHECK_TOOL" "${args[@]}"
}

wm_cascade_add_exit() {
  local manifest="" display_name="" sort_order=100 allow_private=0 dry_run=0 state transaction port path candidate clients outbound desired inbound_id exit_id route_id route_display_name inbound_tag outbound_tag rule_tag xray_before prepared_subs sub_metadata sub_backup subscription_candidate
  while [[ $# -gt 0 ]]; do case "$1" in
    --manifest) manifest="${2:-}"; shift 2;; --display-name) display_name="${2:-}"; shift 2;; --sort-order) sort_order="${2:-}"; shift 2;; --allow-private-target) allow_private=1; shift;; --dry-run) dry_run=1; shift;; *) wm_fail "Unknown cascade add-exit option: $1";; esac; done
  [[ -f "$manifest" ]] || wm_fail "Join manifest not found: $manifest"
  [[ "$sort_order" =~ ^[0-9]+$ ]] || wm_fail "--sort-order must be a non-negative integer"
  wm_lock_mutation "cascade-add-exit"; wm_load_config; wm_require_entry_role
  local validate_args=(--config "$WM_CONFIG_JSON" --manifest "$manifest"); (( allow_private == 1 )) && validate_args+=(--allow-private)
  python3 "$WM_CASCADE_TOOL" validate "${validate_args[@]}" || wm_fail "Join manifest validation failed"
  state="$(python3 "$WM_CASCADE_TOOL" inspect --config "$WM_CONFIG_JSON" --manifest "$manifest")" || wm_fail "Could not inspect existing Exit state"
  if [[ "$state" == "same" ]]; then wm_success "Exit is already imported; no changes required"; return 0; fi
  [[ "$state" == "new" ]] || wm_fail "Exit id already exists with different credentials; rotate explicitly instead of overwriting"
  exit_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["exit"]["id"])' "$manifest")"
  if (( dry_run == 1 )); then wm_info "Plan: validate ${exit_id}, create route inbound/outbound/rule, render nginx, commit desired state"; return 0; fi
  wm_transaction_begin "cascade-add-exit"; transaction="$WM_ACTIVE_TRANSACTION"
  port="$(wm_random_port 21000 21999)"; path="/api/$(wm_random_alnum 2)/$(wm_random_alnum 18)/"; candidate="$transaction/config.candidate.json"; clients="$transaction/clients.json"; outbound="$transaction/outbound.json"; desired="$transaction/inbound.json"; xray_before="$transaction/xray.before.json"
  local prepare_args=(--config "$WM_CONFIG_JSON" --manifest "$manifest" --candidate "$candidate" --clients "$clients" --outbound "$outbound" --path "$path" --port "$port" --sort-order "$sort_order"); [[ -n "$display_name" ]] && prepare_args+=(--display-name "$display_name")
  python3 "$WM_CASCADE_TOOL" prepare "${prepare_args[@]}" || wm_fail "Could not build Entry candidate state"
  route_id="$(EXIT_ID="$exit_id" python3 -c 'import hashlib,os; v=os.environ["EXIT_ID"]; p="route-"; print(p+v if len(p+v)<=48 else p+v[:48-len(p)-8]+"-"+hashlib.sha256(v.encode()).hexdigest()[:7])')"; inbound_tag="wm-route-${exit_id}"; outbound_tag="wm-exit-${exit_id}"; rule_tag="wm-rule-${exit_id}"
  route_display_name="$(python3 -c 'import json,sys; print(next(r["display_name"] for r in json.load(open(sys.argv[1],encoding="utf-8"))["routes"] if r["id"]==sys.argv[2]))' "$candidate" "$route_id")"
  python3 "$WM_INBOUND_TOOL" build --tag "$inbound_tag" --remark "$route_display_name" --port "$port" --path "$path" --host "$DOMAIN" --clients "$clients" --public-domain "$DOMAIN" --fingerprint "$FINGERPRINT" --output "$desired"
  wm_xray_get_template "$xray_before" || wm_fail "Could not capture Xray template before mutation"
  inbound_id="$(wm_inbound_reconcile "$desired")" || wm_fail "Could not create and verify route inbound"
  python3 "$WM_CASCADE_TOOL" finalize --candidate "$candidate" --route-id "$route_id" --inbound-id "$inbound_id" || wm_fail "Could not finalize Entry desired state"
  wm_xray_apply_managed_route "$outbound" "$inbound_tag" "$outbound_tag" "$rule_tag" || wm_fail "Could not apply and verify Exit outbound routing"
  wm_runtime_prepare_subscriptions "$candidate" "$transaction" || wm_fail "Could not prepare route subscription state"
  wm_runtime_apply_public_state "$transaction" || wm_fail "Public subscriptions failed validation; transaction will be rolled back"
  wm_atomic_install_json "$WM_RUNTIME_CANDIDATE" "$WM_CONFIG_JSON"; wm_export_config_env_from_json; wm_transaction_commit "$transaction"
  wm_success "Exit imported and route verified: ${exit_id}"; wm_info "Keep or securely delete the join manifest after confirming service"
}

wm_cascade_command() {
  case "${1:-}" in
    add-exit) shift; wm_cascade_add_exit "$@";;
    auto) shift; wm_auto_command "$@";;
    list|status) shift; wm_runtime_status "$@";;
    health) shift; wm_runtime_health "$@";;
    verify-e2e) shift; wm_cascade_verify_e2e "$@";;
    remove-exit) shift; wm_cascade_remove_exit "$@";;
    *) wm_fail "Usage: wavemesh cascade add-exit --manifest FILE | auto create|status|health|enable|disable|override | list | status [--json] | health [--exit-id ID] [--json] | verify-e2e [--json] | remove-exit --exit-id ID [--force]";;
  esac
}
