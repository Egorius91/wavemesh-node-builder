#!/usr/bin/env bash

WM_AUTO_TOOL="${WM_AUTO_TOOL:-$WM_LIB_DIR/lib/auto_route.py}"
WM_AUTO_HEALTH_TOOL="${WM_AUTO_HEALTH_TOOL:-$WM_LIB_DIR/lib/auto_health.py}"
WM_AUTO_OVERRIDE_FILE="${WM_AUTO_OVERRIDE_FILE:-$WM_STATE_DIR/auto-overrides.json}"
WM_PREPARED_TOOL="$WM_LIB_DIR/lib/prepared_channels.py"

# One local mutation owner. Repeated health ticks belong to the existing Agent
# integration; this command never starts a provider operation or edits rights.
wm_auto_prepared() {
  local action="${1:-}" auto_id="auto-europe" plan="" runtime_view="" effective_config="" observations="" engine="/usr/local/x-ui/bin/xray-linux-amd64"
  local database="" process="" credential_file=""
  [[ "$action" == install || "$action" == reconcile ]] || wm_fail "Usage: wavemesh cascade auto prepared install --plan FILE | reconcile [--id ID]"
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --id) auto_id="${2:-}"; shift 2 ;;
      --plan) plan="${2:-}"; shift 2 ;;
      --runtime-view) runtime_view="${2:-}"; shift 2 ;;
      --effective-config) effective_config="${2:-}"; shift 2 ;;
      --observations) observations="${2:-}"; shift 2 ;;
      --database) database="${2:-}"; shift 2 ;;
      --process-config) process="${2:-}"; shift 2 ;;
      --credential-file) credential_file="${2:-}"; shift 2 ;;
      *) wm_fail "Unknown prepared route option" ;;
    esac
  done
  [[ "$action" != install || -n "$plan" ]] || wm_fail "Prepared route install requires a private plan"
  if [[ "$action" == install ]]; then
    [[ -n "$runtime_view" ]] || wm_fail "Prepared routes require an independent settled process/effective-config collector view"
    [[ -n "$effective_config" ]] || wm_fail "Prepared routes require full qualified executing config separately from the stored panel template"
  else
    [[ -z "$runtime_view" && -z "$effective_config" ]] || wm_fail "Prepared reconcile collects executing evidence internally"
    [[ -n "$database" && -n "$process" && -n "$credential_file" ]] || wm_fail "Prepared reconcile requires admitted database/process paths and an existing private bearer file"
  fi
  [[ "$action" != reconcile || -n "$observations" ]] || wm_fail "Prepared reconcile requires collector-qualified observationv3"
  wm_lock_mutation "auto-prepared-$action"; wm_load_config; wm_require_entry_role
  if [[ "$action" == reconcile ]]; then
    wm_transaction_begin "auto-prepared-reconcile" prepared-v1
    local owning_transaction="$WM_ACTIVE_TRANSACTION"
    python3 "$WM_LIB_DIR/lib/prepared_reconcile.py" --transaction "$owning_transaction" --route-id "route-auto-${auto_id}" \
      --config "$WM_CONFIG_JSON" --runtime "$WM_RUNTIME_JSON" --database "$database" --process "$process" \
      --observations "$observations" --credential-file "$credential_file" || wm_fail "Prepared reconcile is not verified; retain journal"
    wm_transaction_commit "$owning_transaction"
    wm_success "Prepared reconcile committed; current path readiness remains independently qualified"
    return 0
  fi
  wm_transaction_begin "auto-prepared-$action"
  local transaction="$WM_ACTIVE_TRANSACTION" route_id="route-auto-${auto_id}"
  python3 - "$transaction" "$WM_LIB_DIR/lib" <<'PY' || wm_fail "Could not retain prepared no-effect guard"
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from transaction_state import atomic_json
atomic_json(Path(sys.argv[1])/'prepared.no-effect.json', {'status':'NO_CONFIG_MUTATION_DISPATCHED'})
PY
  local args=("$action" --config "$WM_CONFIG_JSON" --runtime "$WM_RUNTIME_JSON" --runtime-view "$runtime_view" --effective-config "$effective_config" --route-id "$route_id" --template "$transaction/xray.before.json" --engine-binary "$engine" --output "$transaction")
  if [[ "$action" == install ]]; then
    args+=(--plan "$plan")
  else
    # The existing Agent collector must bind full-path samples to the ACTUAL
    # executing instance before/after. The old template-only probe cannot mint
    # that proof; missing production collection remains default closed.
    args+=(--observations "$observations")
  fi
  python3 "$WM_PREPARED_TOOL" "${args[@]}" || wm_fail "Prepared route compilation/native validation failed"
  if python3 - "$transaction/prepared.update.json" <<'PY'
import json,sys
sys.exit(0 if json.load(open(sys.argv[1]))['update_required'] is False else 1)
PY
  then
    python3 - "$transaction/prepared.config.json" "$WM_CONFIG_JSON" <<'PY' || wm_fail "Prepared config-only change awaits coordinated metadata commit; no engine POST dispatched"
import json,sys
sys.exit(0 if json.load(open(sys.argv[1]))==json.load(open(sys.argv[2])) else 1)
PY
    # Only the atomic health file changes on a routine no-op tick. No panel
    # POST, config rewrite, service restart or credential mutation is needed.
    wm_atomic_install_json "$transaction/prepared.runtime.json" "$WM_RUNTIME_JSON"
    wm_transaction_commit "$transaction"
    wm_success "Prepared EU-only health refreshed without engine update"
    return 0
  fi
  python3 - "$transaction" "$WM_LIB_DIR/lib" <<'PY' || wm_fail "Could not retain prepared dispatch guard"
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from transaction_state import atomic_json
from prepared_channels import digest
directory=Path(sys.argv[1])
atomic_json(directory/'prepared.apply.pending.json', {'status':'UNKNOWN_UNTIL_QUALIFIED_ENGINE_TRANSITION','candidate_sha256':digest(json.loads((directory/'prepared.xray.json').read_text()))})
PY
  rm -f "$transaction/prepared.no-effect.json"
  wm_xray_apply_template "$transaction/prepared.xray.json" || wm_fail "Prepared route apply failed"
  wm_xray_get_template "$transaction/prepared.readback.json" || wm_fail "Prepared route readback failed"
  # HTTP success plus mutable panel GetConfig is not a settled engine proof.
  # Retain candidate/snapshot/pending debt for the owning qualifier. Never
  # relabel pre-apply samples as observations of a replacement process.
  wm_fail "Prepared update retained pending qualified engine transition; collector/settlement integration is not installed"
}

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
  printf '[]\n' > "$clients"
  route_id="route-auto-${auto_id}"; inbound_tag="wm-route-auto-${auto_id}"; balancer_tag="wm-balancer-${auto_id}"; rule_tag="wm-rule-auto-${auto_id}"
  selectors="$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1],encoding="utf-8")); print(",".join(next(x["selector"] for x in c["balancers"] if x["id"]==sys.argv[2])))' "$candidate" "$auto_id")"
  python3 "$WM_INBOUND_TOOL" build --tag "$inbound_tag" --remark="--!${inbound_tag}" --port "$port" --path "$path" --host "$DOMAIN" --clients "$clients" --public-domain "$DOMAIN" --fingerprint "$FINGERPRINT" --output "$desired"
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
  printf '%s' "$description" | python3 -c 'import json,sys; sys.exit(1 if json.load(sys.stdin).get("prepared") else 0)' || wm_fail "Prepared routes cannot use legacy overrides"
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
  local published="$1" auto_id="auto-europe" transaction candidate desired inbound_id; shift
  while [[ $# -gt 0 ]]; do case "$1" in --id) auto_id="${2:-}"; shift 2 ;; *) wm_fail "Usage: wavemesh cascade auto publish|unpublish [--id ID]" ;; esac; done
  wm_lock_mutation "auto-route-publish"; wm_load_config; wm_require_entry_role
  wm_transaction_begin "auto-route-publish"; transaction="$WM_ACTIVE_TRANSACTION"; candidate="$transaction/config.auto.publish.json"
  python3 "$WM_AUTO_TOOL" set-published --config "$WM_CONFIG_JSON" --output "$candidate" --id "$auto_id" --published "$published" || wm_fail "Could not update Auto Route publication state"
  desired="$transaction/auto.inbound.json"
  python3 "$WM_RUNTIME_TOOL" inbound-artifact --config "$candidate" --route-id "route-auto-${auto_id}" --output "$desired" || wm_fail "Could not build Auto Route publication inbound"
  inbound_id="$(wm_inbound_reconcile "$desired")" || wm_fail "Could not reconcile Auto Route client associations"
  wm_runtime_prepare_subscriptions "$candidate" "$transaction" || wm_fail "Could not prepare Auto Route public state"
  wm_runtime_apply_public_state "$transaction" || wm_fail "Public subscriptions failed validation"
  wm_atomic_install_json "$WM_RUNTIME_CANDIDATE" "$WM_CONFIG_JSON"; wm_export_config_env_from_json; wm_transaction_commit "$transaction"
  wm_success "Auto Route ${auto_id} $([[ "$published" == "true" ]] && echo published || echo unpublished)"
}

wm_auto_command() {
  case "${1:-}" in
    prepared) shift; wm_auto_prepared "$@" ;;
    create) shift; wm_auto_create "$@" ;; status|list) shift; wm_auto_status "$@" ;; health) shift; wm_auto_health "$@" ;; enable) shift; wm_auto_toggle true "$@" ;; disable) shift; wm_auto_toggle false "$@" ;; override) shift; wm_auto_override "$@" ;; publish) shift; wm_auto_publish_toggle true "$@" ;; unpublish) shift; wm_auto_publish_toggle false "$@" ;; *) wm_fail "Usage: wavemesh cascade auto create [...] | status [--json] | health [--json] | enable|disable [--id ID] | override [--id ID] --exit-id EXIT_ID|clear | publish|unpublish [--id ID]" ;; esac
}
