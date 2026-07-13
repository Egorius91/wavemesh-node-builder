#!/usr/bin/env bash

wm_require_entry_role() { [[ "$NODE_ROLE" == "entry" ]] || wm_fail "This command requires an entry node"; }

wm_cascade_add_exit() {
  local manifest="" display_name="" sort_order=100 allow_private=0 dry_run=0 state transaction port path candidate clients outbound desired inbound_id exit_id route_id inbound_tag outbound_tag rule_tag xray_before prepared_subs sub_metadata sub_backup subscription_candidate
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
  python3 "$WM_INBOUND_TOOL" build --tag "$inbound_tag" --port "$port" --path "$path" --host "$DOMAIN" --clients "$clients" --public-domain "$DOMAIN" --fingerprint "$FINGERPRINT" --output "$desired"
  wm_xray_get_template "$xray_before" || wm_fail "Could not capture Xray template before mutation"
  inbound_id="$(wm_inbound_reconcile "$desired")" || wm_fail "Could not create and verify route inbound"
  python3 "$WM_CASCADE_TOOL" finalize --candidate "$candidate" --route-id "$route_id" --inbound-id "$inbound_id" || wm_fail "Could not finalize Entry desired state"
  wm_xray_apply_managed_route "$outbound" "$inbound_tag" "$outbound_tag" "$rule_tag" || wm_fail "Could not apply and verify Exit outbound routing"
  prepared_subs="$transaction/subscriptions"; sub_metadata="$transaction/subscriptions.json"; sub_backup="$transaction/subscriptions.before"; subscription_candidate="$transaction/config.subscription.json"; mkdir -p "$prepared_subs" "$sub_backup"
  wm_subscription_prepare "$candidate" "$subscription_candidate" "$prepared_subs" "$sub_metadata" || wm_fail "Could not render route subscriptions"
  candidate="$subscription_candidate"; wm_subscription_install_files "$prepared_subs" "$sub_backup"
  wm_nginx_apply_desired "$candidate" "$transaction" || wm_fail "nginx rejected route; transaction will be rolled back"
  wm_subscription_validate_public "$sub_metadata" || wm_fail "Public subscriptions failed validation; transaction will be rolled back"
  wm_atomic_install_json "$candidate" "$WM_CONFIG_JSON"; wm_export_config_env_from_json; wm_transaction_commit "$transaction"
  wm_success "Exit imported and route verified: ${exit_id}"; wm_info "Keep or securely delete the join manifest after confirming service"
}

wm_cascade_command() {
  case "${1:-}" in
    add-exit) shift; wm_cascade_add_exit "$@";;
    list|status) shift; wm_runtime_status "$@";;
    health) shift; wm_runtime_health "$@";;
    remove-exit) shift; wm_cascade_remove_exit "$@";;
    *) wm_fail "Usage: wavemesh cascade add-exit --manifest FILE | list | status [--json] | health [--exit-id ID] [--json] | remove-exit --exit-id ID [--force]";;
  esac
}
