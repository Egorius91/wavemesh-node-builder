#!/usr/bin/env bash

WM_AUTO_TOOL="${WM_AUTO_TOOL:-$WM_LIB_DIR/lib/auto_route.py}"
WM_AUTO_HEALTH_TOOL="${WM_AUTO_HEALTH_TOOL:-$WM_LIB_DIR/lib/auto_health.py}"
WM_AUTO_OVERRIDE_FILE="${WM_AUTO_OVERRIDE_FILE:-$WM_STATE_DIR/auto-overrides.json}"

wm_auto_create() {
  local auto_id="auto-europe" display_name="⚡ RU -> Auto Europe" exit_ids="" sort_order=50 dry_run=0
  local transaction port path candidate clients desired inbound_id route_id inbound_tag balancer_tag rule_tag selectors
  while [[ $# -gt 0 ]]; do case "$1" in --id) auto_id="${2:-}"; shift 2 ;; --display-name) display_name="${2:-}"; shift 2 ;; --exit-id) [[ -n "${2:-}" ]] || wm_fail "--exit-id requires a value"; exit_ids="${exit_ids:+${exit_ids},}${2}"; shift 2 ;; --sort-order) sort_order="${2:-}"; shift 2 ;; --dry-run) dry_run=1; shift ;; *) wm_fail "Unknown Auto Route create option: $1" ;; esac; done
  [[ "$sort_order" =~ ^[0-9]+$ ]] || wm_fail "--sort-order must be a non-negative integer"
  wm_lock_mutation "auto-route-create"; wm_load_config; wm_require_entry_role
  if (( dry_run == 1 )); then wm_info "Plan: create unpublished Auto Route ${auto_id}, local inbound, leastPing balancer, observatory, and routing rule"; return 0; fi
  wm_transaction_begin "auto-route-create"; transaction="$WM_ACTIVE_TRANSACTION"
  port="$(wm_random_port 21000 21999)"; path="/api/auto/$(wm_random_alnum 18)/"; candidate="$transaction/config.auto.candidate.json"; clients="$transaction/auto.clients.json"; desired="$transaction/auto.inbound.json"
  python3 "$WM_AUTO_TOOL" prepare --config "$WM_CONFIG_JSON" --candidate "$candidate" --clients "$clients" --id "$auto_id" --display-name "$display_name" --exit-ids "$exit_ids" --port "$port" --path "$path" --sort-order "$sort_order" || wm_fail "Could not build Auto Route desired state"
  route_id="route-auto-${auto_id}"; inbound_tag="wm-route-auto-${auto_id}"; balancer_tag="wm-balancer-${auto_id}"; rule_tag="wm-rule-auto-${auto_id}"
  selectors="$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1],encoding="utf-8")); print(",".join(next(x["selector"] for x in c["balancers"] if x["id"]==sys.argv[2])))' "$candidate" "$auto_id")"
  python3 "$WM_INBOUND_TOOL" build --tag "$inbound_tag" --remark "--!${display_name}" --port "$port" --path "$path" --host "$DOMAIN" --clients "$clients" --public-domain "$DOMAIN" --fingerprint "$FINGERPRINT" --output "$desired"
  inbound_id="$(wm_inbound_reconcile "$desired")" || wm_fail "Could not create and verify Auto Route inbound"
  python3 "$WM_AUTO_TOOL" finalize --candidate "$candidate" --route-id "$route_id" --inbound-id "$inbound_id" || wm_fail "Could not finalize Auto Route desired state"
  wm_xray_apply_managed_balancer "$selectors" "$inbound_tag" "$balancer_tag" "$rule_tag" leastPing || wm_fail "Could not apply and verify Auto Route balancer"
  wm_atomic_install_json "$candidate" "$WM_CONFIG_JSON"; wm_export_config_env_from_json; wm_transaction_commit "$transaction"
  wm_success "Auto Route control plane created: ${auto_id}"; wm_info "The Auto profile is intentionally unpublished until explicitly published"
}

wm_auto_status() {
  local as_json=0
  while [[ $# -gt 0 ]]; do case "$1" in --json) as_json=1; shift ;; *) wm_fail "Usage: wavemesh cascade auto status [--json]" ;; esac; done
  wm_load_config; wm_require_entry_role
  JSON_OUTPUT="$as_json" python3 - "$WM_CONFIG_JSON" "$WM_AUTO_OVERRIDE_FILE" <<'PY'
import json,os,sys
config=json.load(open(sys.argv[1],encoding="utf-8")); overrides={}
try: overrides=json.load(open(sys.argv[2],encoding="utf-8"))
except (FileNotFoundError,json.JSONDecodeError): pass
balancers={item.get("id"):item for item in config.get("balancers",[])}; rows=[]
for route in config.get("routes",[]):
    if route.get("kind")!="auto": continue
    balancer=balancers.get(route.get("balancer_id"),{})
    rows.append({"id":route.get("id"),"display_name":route.get("display_name"),"enabled":route.get("enabled",True),"published":route.get("presentation",{}).get("published",False),"strategy":balancer.get("strategy"),"selectors":balancer.get("selector",[]),"override_exit_id":overrides.get(route.get("balancer_id")),"inbound_id":route.get("entry",{}).get("inbound_id"),"inbound_tag":route.get("entry",{}).get("inbound_tag"),"balancer_tag":route.get("routing",{}).get("balancer_tag")})
if os.environ["JSON_OUTPUT"]=="1": print(json.dumps({"auto_routes":rows},indent=2,ensure_ascii=False))
elif not rows: print("No Auto Routes")
else:
    print("AUTO ROUTE\tSTATE\tPUBLISHED\tSTRATEGY\tEXITS\tOVERRIDE")
    for row in rows: print(f"{row['display_name']}\t{'enabled' if row['enabled'] else 'disabled'}\t{str(row['published']).lower()}\t{row['strategy']}\t{len(row['selectors'])}\t{row['override_exit_id'] or '-'}")
PY
}

wm_auto_health() {
  local as_json=0 inbounds template
  while [[ $# -gt 0 ]]; do case "$1" in --json) as_json=1; shift ;; *) wm_fail "Usage: wavemesh cascade auto health [--json]" ;; esac; done
  wm_load_config; wm_require_entry_role; inbounds="$(mktemp)"; template="$(mktemp)"
  wm_xui_request_success GET /panel/api/inbounds/list none > "$inbounds" || { rm -f "$inbounds" "$template"; wm_fail "Could not read 3X-UI inbounds"; }
  wm_xray_get_template "$template" || { rm -f "$inbounds" "$template"; wm_fail "Could not read Xray template"; }
  [[ -f "$WM_AUTO_OVERRIDE_FILE" ]] || printf '{}\n' > "$WM_AUTO_OVERRIDE_FILE"
  local args=(--config "$WM_CONFIG_JSON" --runtime "$WM_RUNTIME_JSON" --inbounds "$inbounds" --template "$template" --overrides "$WM_AUTO_OVERRIDE_FILE"); (( as_json == 1 )) && args+=(--json)
  python3 "$WM_AUTO_HEALTH_TOOL" "${args[@]}"; local rc=$?; rm -f "$inbounds" "$template"; return "$rc"
}

wm_auto_toggle() {
  local enabled="$1" auto_id="auto-europe" transaction candidate description inbound_id inbound_tag balancer_tag rule_tag selectors; shift
  while [[ $# -gt 0 ]]; do case "$1" in --id) auto_id="${2:-}"; shift 2 ;; *) wm_fail "Usage: wavemesh cascade auto enable|disable [--id ID]" ;; esac; done
  wm_lock_mutation "auto-route-toggle"; wm_load_config; wm_require_entry_role
  description="$(python3 "$WM_AUTO_TOOL" describe --config "$WM_CONFIG_JSON" --id "$auto_id")" || wm_fail "Auto Route not found: $auto_id"
  if [[ "$enabled" == "false" ]] && [[ "$(printf '%s' "$description" | python3 -c 'import json,sys; print(str(json.load(sys.stdin).get("published",False)).lower())')" == "true" ]]; then wm_fail "Unpublish Auto Route before disabling it"; fi
  inbound_id="$(printf '%s' "$description" | python3 -c 'import json,sys; print(json.load(sys.stdin)["inbound_id"])')"; inbound_tag="$(printf '%s' "$description" | python3 -c 'import json,sys; print(json.load(sys.stdin)["inbound_tag"])')"; balancer_tag="$(printf '%s' "$description" | python3 -c 'import json,sys; print(json.load(sys.stdin)["balancer_tag"])')"; rule_tag="$(printf '%s' "$description" | python3 -c 'import json,sys; print(json.load(sys.stdin)["rule_tag"])')"; selectors="$(printf '%s' "$description" | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)["selectors"]))')"
  wm_transaction_begin "auto-route-toggle"; transaction="$WM_ACTIVE_TRANSACTION"; candidate="$transaction/config.auto.toggle.json"
  python3 "$WM_AUTO_TOOL" set-enabled --config "$WM_CONFIG_JSON" --output "$candidate" --id "$auto_id" --enabled "$enabled" || wm_fail "Could not update Auto Route desired state"
  if [[ "$enabled" == "true" ]]; then wm_inbound_set_enabled "$inbound_id" true || wm_fail "Could not enable Auto Route inbound"; wm_xray_apply_managed_balancer "$selectors" "$inbound_tag" "$balancer_tag" "$rule_tag" leastPing || wm_fail "Could not enable Auto Route balancer"; else wm_inbound_set_enabled "$inbound_id" false || wm_fail "Could not disable Auto Route inbound"; wm_xray_remove_managed_balancer "$inbound_tag" "$balancer_tag" "$rule_tag" || wm_fail "Could not remove Auto Route balancer"; fi
  wm_atomic_install_json "$candidate" "$WM_CONFIG_JSON"; wm_export_config_env_from_json; wm_transaction_commit "$transaction"; wm_success "Auto Route ${auto_id} $([[ "$enabled" == "true" ]] && echo enabled || echo disabled)"
}

wm_auto_override() {
  local auto_id="auto-europe" exit_id="" clear=0 description selectors inbound_tag balancer_tag rule_tag strategy
  while [[ $# -gt 0 ]]; do case "$1" in --id) auto_id="${2:-}"; shift 2 ;; --exit-id) exit_id="${2:-}"; shift 2 ;; clear) clear=1; shift ;; *) wm_fail "Usage: wavemesh cascade auto override [--id ID] --exit-id EXIT_ID | clear" ;; esac; done
  wm_lock_mutation "auto-route-override"; wm_load_config; wm_require_entry_role
  description="$(python3 "$WM_AUTO_TOOL" describe --config "$WM_CONFIG_JSON" --id "$auto_id")" || wm_fail "Auto Route not found: $auto_id"
  inbound_tag="$(printf '%s' "$description" | python3 -c 'import json,sys; print(json.load(sys.stdin)["inbound_tag"])')"; balancer_tag="$(printf '%s' "$description" | python3 -c 'import json,sys; print(json.load(sys.stdin)["balancer_tag"])')"; rule_tag="$(printf '%s' "$description" | python3 -c 'import json,sys; print(json.load(sys.stdin)["rule_tag"])')"; strategy="$(printf '%s' "$description" | python3 -c 'import json,sys; print(json.load(sys.stdin)["strategy"])')"
  if (( clear == 1 )); then selectors="$(printf '%s' "$description" | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)["selectors"]))')"; else [[ -n "$exit_id" ]] || wm_fail "--exit-id is required"; selectors="$(EXIT_ID="$exit_id" python3 - "$WM_CONFIG_JSON" <<'PY'
import json,os,sys
cfg=json.load(open(sys.argv[1],encoding="utf-8")); e=next((x for x in cfg.get("exits",[]) if x.get("id")==os.environ["EXIT_ID"] and x.get("enabled",True)),None)
if not e: raise SystemExit(1)
print(e.get("xray",{}).get("outbound_tag",""))
PY
)" || wm_fail "Unknown or disabled Exit: $exit_id"; [[ "$selectors" == wm-exit-* ]] || wm_fail "Exit has no managed outbound"; fi
  wm_xray_apply_managed_balancer "$selectors" "$inbound_tag" "$balancer_tag" "$rule_tag" "$strategy" || wm_fail "Could not apply Auto Route override"
  AUTO_ID="$auto_id" EXIT_ID="$exit_id" CLEAR="$clear" FILE="$WM_AUTO_OVERRIDE_FILE" python3 - <<'PY'
import json,os,pathlib
path=pathlib.Path(os.environ["FILE"]); data={}
try: data=json.loads(path.read_text())
except (FileNotFoundError,json.JSONDecodeError): pass
if os.environ["CLEAR"]=="1": data.pop(os.environ["AUTO_ID"],None)
else: data[os.environ["AUTO_ID"]]=os.environ["EXIT_ID"]
path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(data,indent=2)+"\n"); path.chmod(0o600)
PY
  (( clear == 1 )) && wm_success "Auto Route override cleared: ${auto_id}" || wm_success "Auto Route ${auto_id} pinned to ${exit_id}"
}

wm_auto_publish_toggle() {
  local published="$1" auto_id="auto-europe" json_output=0 transaction candidate prepared_subs sub_metadata sub_backup subscription_candidate inbound_id display_name target_remark; shift
  while [[ $# -gt 0 ]]; do case "$1" in --id) auto_id="${2:-}"; shift 2 ;; --json) json_output=1; shift ;; *) wm_fail "Usage: wavemesh cascade auto publish|unpublish [--id ID] [--json]" ;; esac; done
  wm_lock_mutation "auto-route-publish"; wm_load_config; wm_require_entry_role
  wm_transaction_begin "auto-route-publish"; transaction="$WM_ACTIVE_TRANSACTION"; candidate="$transaction/config.auto.publish.json"
  python3 "$WM_AUTO_TOOL" set-published --config "$WM_CONFIG_JSON" --output "$candidate" --id "$auto_id" --published "$published" || wm_fail "Could not update Auto Route publication state"
  read -r inbound_id display_name < <(python3 - "$candidate" "$auto_id" <<'PY'
import json,sys
r=next(x for x in json.load(open(sys.argv[1],encoding="utf-8"))["routes"] if x.get("kind")=="auto" and (x.get("auto_id")==sys.argv[2] or x.get("id")=="route-auto-"+sys.argv[2]))
print(r["entry"]["inbound_id"],r["display_name"])
PY
)
  target_remark="$display_name"; [[ "$published" == "true" ]] || target_remark="--!${display_name}"
  wm_inbound_set_remark "$inbound_id" "$target_remark" || wm_fail "Could not update Auto Route visibility"
  prepared_subs="$transaction/subscriptions"; sub_metadata="$transaction/subscriptions.json"; sub_backup="$transaction/subscriptions.before"; subscription_candidate="$transaction/config.subscription.json"; mkdir -p "$prepared_subs" "$sub_backup"
  if wm_subscription_backend_is_native "$candidate"; then
    cp "$candidate" "$subscription_candidate"; printf '[]\n' > "$sub_metadata"
  else
    wm_subscription_prepare "$candidate" "$subscription_candidate" "$prepared_subs" "$sub_metadata" || wm_fail "Could not render subscriptions"
    wm_subscription_install_files "$prepared_subs" "$sub_backup"
  fi
  candidate="$subscription_candidate"
  wm_nginx_apply_desired "$candidate" "$transaction" || wm_fail "nginx rejected Auto Route publication"
  if wm_subscription_backend_is_native "$candidate"; then wm_subscription_validate_native "$candidate" || wm_fail "Native subscriptions failed validation"; else wm_subscription_validate_public "$sub_metadata" || wm_fail "Public subscriptions failed validation"; fi
  wm_atomic_install_json "$candidate" "$WM_CONFIG_JSON"; wm_export_config_env_from_json; wm_transaction_commit "$transaction"
  if (( json_output == 1 )); then
    AUTO_ID="$auto_id" PUBLISHED="$published" INBOUND_ID="$inbound_id" DISPLAY_NAME="$display_name" python3 - <<'PY'
import json,os
print(json.dumps({"auto_id":os.environ["AUTO_ID"],"published":os.environ["PUBLISHED"]=="true","inbound_id":int(os.environ["INBOUND_ID"]),"display_name":os.environ["DISPLAY_NAME"]},ensure_ascii=False))
PY
  else
    wm_success "Auto Route ${auto_id} $([[ "$published" == "true" ]] && echo published || echo unpublished)"
  fi
}

wm_auto_command() {
  case "${1:-}" in
    create) shift; wm_auto_create "$@" ;; status|list) shift; wm_auto_status "$@" ;; health) shift; wm_auto_health "$@" ;; enable) shift; wm_auto_toggle true "$@" ;; disable) shift; wm_auto_toggle false "$@" ;; override) shift; wm_auto_override "$@" ;; publish) shift; wm_auto_publish_toggle true "$@" ;; unpublish) shift; wm_auto_publish_toggle false "$@" ;; *) wm_fail "Usage: wavemesh cascade auto create [...] | status [--json] | health [--json] | enable|disable [--id ID] | override [--id ID] --exit-id EXIT_ID|clear | publish|unpublish [--id ID] [--json]" ;; esac
}
