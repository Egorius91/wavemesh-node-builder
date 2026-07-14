#!/usr/bin/env bash

WM_AUTO_TOOL="${WM_AUTO_TOOL:-$WM_LIB_DIR/lib/auto_route.py}"

wm_auto_create() {
  local auto_id="auto-europe" display_name="⚡ RU -> Auto Europe" exit_ids="" sort_order=50 dry_run=0
  local transaction port path candidate clients desired inbound_id route_id inbound_tag balancer_tag rule_tag selectors
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --id) auto_id="${2:-}"; shift 2 ;;
      --display-name) display_name="${2:-}"; shift 2 ;;
      --exit-id)
        [[ -n "${2:-}" ]] || wm_fail "--exit-id requires a value"
        exit_ids="${exit_ids:+${exit_ids},}${2}"
        shift 2
        ;;
      --sort-order) sort_order="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) wm_fail "Unknown Auto Route create option: $1" ;;
    esac
  done
  [[ "$sort_order" =~ ^[0-9]+$ ]] || wm_fail "--sort-order must be a non-negative integer"
  wm_lock_mutation "auto-route-create"
  wm_load_config
  wm_require_entry_role
  if (( dry_run == 1 )); then
    wm_info "Plan: create unpublished Auto Route ${auto_id}, local inbound, leastPing balancer, observatory, and routing rule"
    return 0
  fi

  wm_transaction_begin "auto-route-create"
  transaction="$WM_ACTIVE_TRANSACTION"
  port="$(wm_random_port 21000 21999)"
  path="/api/auto/$(wm_random_alnum 18)/"
  candidate="$transaction/config.auto.candidate.json"
  clients="$transaction/auto.clients.json"
  desired="$transaction/auto.inbound.json"

  python3 "$WM_AUTO_TOOL" prepare \
    --config "$WM_CONFIG_JSON" \
    --candidate "$candidate" \
    --clients "$clients" \
    --id "$auto_id" \
    --display-name "$display_name" \
    --exit-ids "$exit_ids" \
    --port "$port" \
    --path "$path" \
    --sort-order "$sort_order" || wm_fail "Could not build Auto Route desired state"

  route_id="route-auto-${auto_id}"
  inbound_tag="wm-route-auto-${auto_id}"
  balancer_tag="wm-balancer-${auto_id}"
  rule_tag="wm-rule-auto-${auto_id}"
  selectors="$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1],encoding="utf-8")); print(",".join(next(x["selector"] for x in c["balancers"] if x["id"]==sys.argv[2])))' "$candidate" "$auto_id")"

  python3 "$WM_INBOUND_TOOL" build \
    --tag "$inbound_tag" \
    --remark "$display_name" \
    --port "$port" \
    --path "$path" \
    --host "$DOMAIN" \
    --clients "$clients" \
    --public-domain "$DOMAIN" \
    --fingerprint "$FINGERPRINT" \
    --output "$desired"

  inbound_id="$(wm_inbound_reconcile "$desired")" || wm_fail "Could not create and verify Auto Route inbound"
  python3 "$WM_AUTO_TOOL" finalize --candidate "$candidate" --route-id "$route_id" --inbound-id "$inbound_id" || wm_fail "Could not finalize Auto Route desired state"
  wm_xray_apply_managed_balancer "$selectors" "$inbound_tag" "$balancer_tag" "$rule_tag" leastPing || wm_fail "Could not apply and verify Auto Route balancer"

  wm_atomic_install_json "$candidate" "$WM_CONFIG_JSON"
  wm_export_config_env_from_json
  wm_transaction_commit "$transaction"
  wm_success "Auto Route control plane created: ${auto_id}"
  wm_info "The Auto profile is intentionally unpublished until the next Phase 11 increment"
}

wm_auto_status() {
  local as_json=0
  while [[ $# -gt 0 ]]; do case "$1" in --json) as_json=1; shift ;; *) wm_fail "Usage: wavemesh cascade auto status [--json]" ;; esac; done
  wm_load_config
  wm_require_entry_role
  JSON_OUTPUT="$as_json" python3 - "$WM_CONFIG_JSON" <<'PY'
import json,os,sys
config=json.load(open(sys.argv[1],encoding="utf-8"))
balancers={item.get("id"):item for item in config.get("balancers",[])}
rows=[]
for route in config.get("routes",[]):
    if route.get("kind")!="auto": continue
    balancer=balancers.get(route.get("balancer_id"),{})
    rows.append({
        "id":route.get("id"),
        "display_name":route.get("display_name"),
        "enabled":route.get("enabled",True),
        "published":route.get("presentation",{}).get("published",False),
        "strategy":balancer.get("strategy"),
        "selectors":balancer.get("selector",[]),
        "inbound_id":route.get("entry",{}).get("inbound_id"),
        "inbound_tag":route.get("entry",{}).get("inbound_tag"),
        "balancer_tag":route.get("routing",{}).get("balancer_tag"),
    })
if os.environ["JSON_OUTPUT"]=="1":
    print(json.dumps({"auto_routes":rows},indent=2,ensure_ascii=False))
elif not rows:
    print("No Auto Routes")
else:
    print("AUTO ROUTE\tSTATE\tPUBLISHED\tSTRATEGY\tEXITS")
    for row in rows:
        print(f"{row['display_name']}\t{'enabled' if row['enabled'] else 'disabled'}\t{str(row['published']).lower()}\t{row['strategy']}\t{len(row['selectors'])}")
PY
}

wm_auto_command() {
  case "${1:-}" in
    create) shift; wm_auto_create "$@" ;;
    status|list) shift; wm_auto_status "$@" ;;
    *) wm_fail "Usage: wavemesh cascade auto create [--id ID] [--display-name TEXT] [--exit-id ID ...] [--sort-order N] [--dry-run] | status [--json]" ;;
  esac
}
