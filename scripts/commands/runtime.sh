
#!/usr/bin/env bash

WM_RUNTIME_TOOL="${WM_RUNTIME_TOOL:-$WM_LIB_DIR/lib/runtime_state.py}"

wm_require_entry_role() { [[ "$NODE_ROLE" == "entry" ]] || wm_fail "This command requires an entry node"; }

wm_xray_process_running() {
  local exe target name proc_root="${WM_PROC_ROOT:-/proc}"
  for exe in "$proc_root"/[0-9]*/exe; do
    target="$(readlink "$exe" 2>/dev/null)" || continue
    name="${target##*/}"
    case "$name" in
      xray|xray-*) return 0 ;;
    esac
  done
  return 1
}

wm_runtime_status() {
  local as_json=0
  while [[ $# -gt 0 ]]; do case "$1" in --json) as_json=1; shift;; *) wm_fail "Unknown status option: $1";; esac; done
  wm_load_config
  local args=(status --config "$WM_CONFIG_JSON" --runtime "$WM_RUNTIME_JSON")
  (( as_json == 1 )) && args+=(--json)
  python3 "$WM_RUNTIME_TOOL" "${args[@]}"
}

wm_runtime_probe_set() {
  local file="$1" route_id="$2" route_test="$3" test_outbound="$4" latency="$5" outbound="$6" error="$7"
  FILE="$file" ROUTE_ID="$route_id" ROUTE_TEST="$route_test" TEST_OUTBOUND="$test_outbound" LATENCY="$latency" OUTBOUND="$outbound" ERROR="$error" python3 - <<'PY'
import json, os
path=os.environ["FILE"]
data=json.load(open(path,encoding="utf-8"))
data.setdefault("routes",[]).append({
    "route_id":os.environ["ROUTE_ID"],
    "route_test":os.environ["ROUTE_TEST"]=="true",
    "test_outbound":os.environ["TEST_OUTBOUND"]=="true",
    "latency_ms":int(os.environ["LATENCY"]) if os.environ["LATENCY"].isdigit() else None,
    "route_test_outbound":os.environ["OUTBOUND"] or None,
    "error":os.environ["ERROR"] or None,
})
with open(path,"w",encoding="utf-8") as out: json.dump(data,out,separators=(",",":")); out.write("\n")
PY
  chmod 600 "$file"
}

wm_runtime_health() {
  local as_json=0 requested_exit="" transaction inbounds template probes nginx_file api_ok=false api_state=unreachable xui_service=inactive xray_state=stopped panel_bind=exposed bearer_state=invalid nginx_state=inactive tls_state=invalid
  while [[ $# -gt 0 ]]; do case "$1" in --json) as_json=1; shift;; --exit-id) requested_exit="${2:-}"; shift 2;; *) wm_fail "Unknown health option: $1";; esac; done
  wm_load_config; wm_require_entry_role
  if [[ -n "$requested_exit" ]] && ! python3 -c 'import json,sys; cfg=json.load(open(sys.argv[1],encoding="utf-8")); sys.exit(0 if any(x.get("id")==sys.argv[2] for x in cfg.get("exits",[])) else 1)' "$WM_CONFIG_JSON" "$requested_exit"; then wm_fail "Exit not found: ${requested_exit}"; fi
  transaction="$(mktemp -d)"; chmod 700 "$transaction"
  inbounds="$transaction/inbounds.json"; template="$transaction/xray.json"; probes="$transaction/probes.json"; nginx_file="${WM_NGINX_MANAGED_CONF:-/etc/nginx/wavemesh-managed-locations.conf}"
  printf '{"obj":[]}\n' > "$inbounds"; printf '{}\n' > "$template"
  if wm_xui_request_success GET /panel/api/inbounds/list none > "$inbounds"; then api_ok=true; api_state=reachable; fi
  wm_xray_get_template "$template" || printf '{}\n' > "$template"
  systemctl is-active --quiet x-ui && xui_service=active || true
  wm_xray_process_running && xray_state=running || true
  if ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq "^(127\\.0\\.0\\.1|\\[::1\\]):${PANEL_PORT}$"; then panel_bind=loopback; fi
  [[ "${PANEL_TOKEN:-}" != "" ]] && $api_ok && bearer_state=valid || true
  systemctl is-active --quiet nginx && nginx_state=active || true
  openssl x509 -checkend 0 -noout -in "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" >/dev/null 2>&1 && tls_state=valid || true
  XUI_SERVICE="$xui_service" API_STATE="$api_state" XRAY_STATE="$xray_state" PANEL_BIND="$panel_bind" BEARER_STATE="$bearer_state" NGINX_STATE="$nginx_state" TLS_STATE="$tls_state" PROBES="$probes" python3 - <<'PY'
import json,os
data={"control":{"service":os.environ["XUI_SERVICE"],"api":os.environ["API_STATE"],"xray":os.environ["XRAY_STATE"],"panel_bind":os.environ["PANEL_BIND"],"bearer":os.environ["BEARER_STATE"],"nginx":os.environ["NGINX_STATE"],"tls":os.environ["TLS_STATE"]},"routes":[]}
with open(os.environ["PROBES"],"w",encoding="utf-8") as out: json.dump(data,out,separators=(",",":")); out.write("\n")
PY
  chmod 600 "$probes"

  local route_id exit_id inbound_tag outbound_tag inbound_file outbound_file started latency route_ok outbound_ok error
  while IFS=$'\t' read -r route_id exit_id inbound_tag outbound_tag; do
    [[ -n "$route_id" ]] || continue
    inbound_file="$transaction/${route_id}.inbound.json"; outbound_file="$transaction/${route_id}.outbound.json"
    python3 "$WM_RUNTIME_TOOL" artifacts --config "$WM_CONFIG_JSON" --route-id "$route_id" --inbound "$inbound_file" --outbound "$outbound_file" || { wm_runtime_probe_set "$probes" "$route_id" false false "" "$outbound_tag" "could not build managed route"; continue; }
    route_ok=false; outbound_ok=false; error=""; started="$(date +%s%3N)"
    if $api_ok && wm_xray_test_outbound "$outbound_file" "$template"; then outbound_ok=true; else error="testOutbound failed"; fi
    if $api_ok && wm_xray_route_test "$inbound_tag" "$outbound_tag"; then route_ok=true; else [[ -n "$error" ]] && error="$error; routeTest failed" || error="routeTest failed"; fi
    latency=$(( $(date +%s%3N) - started ))
    wm_runtime_probe_set "$probes" "$route_id" "$route_ok" "$outbound_ok" "$latency" "$outbound_tag" "$error"
  done < <(python3 - "$WM_CONFIG_JSON" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1],encoding="utf-8"))
for r in cfg.get("routes",[]):
    if r.get("kind")=="cascade" and r.get("enabled",True): print("\t".join((r["id"],r["exit_id"],r["entry"]["inbound_tag"],r["routing"]["outbound_tag"])))
PY
)

  python3 "$WM_RUNTIME_TOOL" evaluate --config "$WM_CONFIG_JSON" --runtime "$WM_RUNTIME_JSON" --inbounds "$inbounds" --template "$template" --nginx "$nginx_file" --subscriptions "$WM_SUB_DIR" --probes "$probes" --output "$WM_RUNTIME_JSON"
  chmod 600 "$WM_RUNTIME_JSON"
  local args=(status --config "$WM_CONFIG_JSON" --runtime "$WM_RUNTIME_JSON")
  (( as_json == 1 )) && args+=(--json)
  if [[ -n "$requested_exit" ]]; then
    python3 "$WM_RUNTIME_TOOL" status --config "$WM_CONFIG_JSON" --runtime "$WM_RUNTIME_JSON" --json | REQUESTED_EXIT="$requested_exit" JSON_OUTPUT="$as_json" python3 -c 'import json,os,sys; data=json.load(sys.stdin); rows=[r for r in data["routes"] if r["exit_id"]==os.environ["REQUESTED_EXIT"]]; table="ROUTE\tSTATUS\tLATENCY\tOUTBOUND\tLAST CHECK\n"+"\n".join("{}\t{}\t{}\t{}\t{}".format(r["display_name"],r["status"],str(r["latency_ms"])+" ms" if r["latency_ms"] is not None else "-",r["outbound"],r["last_check"] or "-") for r in rows); print(json.dumps({**data,"routes":rows},indent=2,ensure_ascii=False) if os.environ["JSON_OUTPUT"]=="1" else (table if rows else "No cascade routes for Exit"))'
  else
    python3 "$WM_RUNTIME_TOOL" "${args[@]}"
  fi
  rm -rf "$transaction"
}

wm_runtime_prepare_subscriptions() {
  local source="$1" transaction="$2"
  WM_RUNTIME_CANDIDATE="$transaction/config.subscription.json"
  WM_RUNTIME_PREPARED="$transaction/subscriptions"
  WM_RUNTIME_METADATA="$transaction/subscriptions.json"
  WM_RUNTIME_SUB_BACKUP="$transaction/subscriptions.before"
  mkdir -p "$WM_RUNTIME_PREPARED" "$WM_RUNTIME_SUB_BACKUP"
  wm_subscription_prepare "$source" "$WM_RUNTIME_CANDIDATE" "$WM_RUNTIME_PREPARED" "$WM_RUNTIME_METADATA"
}

wm_runtime_apply_public_state() {
  local transaction="$1"
  wm_subscription_install_files "$WM_RUNTIME_PREPARED" "$WM_RUNTIME_SUB_BACKUP"
  wm_nginx_apply_desired "$WM_RUNTIME_CANDIDATE" "$transaction" || return 1
  wm_subscription_validate_public "$WM_RUNTIME_METADATA" || return 1
}

wm_runtime_remove_xray_routes() {
  local config="$1" affected="$2" original="$3" candidate="$4" route_id inbound_tag outbound_tag rule_tag next readback
  wm_xray_get_template "$original" || return 1
  cp "$original" "$candidate"
  while IFS=$'\t' read -r route_id inbound_tag outbound_tag rule_tag; do
    [[ -n "$route_id" ]] || continue
    next="${candidate}.next"
    python3 "$WM_XRAY_TEMPLATE_TOOL" remove --template "$candidate" --inbound-tag "$inbound_tag" --outbound-tag "$outbound_tag" --rule-tag "$rule_tag" --output "$next" || return 1
    mv "$next" "$candidate"
  done < <(python3 - "$config" "$affected" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1],encoding="utf-8")); affected=set(json.load(open(sys.argv[2],encoding="utf-8")))
for r in cfg.get("routes",[]):
    if r.get("id") in affected: print("\t".join((r["id"],r["entry"]["inbound_tag"],r["routing"]["outbound_tag"],r["routing"]["rule_tag"])))
PY
)
  wm_xray_apply_template "$candidate" || return 1
  readback="${candidate}.readback"
  wm_xray_get_template "$readback" || return 1
  python3 - "$config" "$affected" "$readback" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1],encoding="utf-8")); ids=set(json.load(open(sys.argv[2],encoding="utf-8"))); template=json.load(open(sys.argv[3],encoding="utf-8"))
routes=[r for r in cfg.get("routes",[]) if r.get("id") in ids]
outbounds={x.get("tag") for x in template.get("outbounds",[])}; rules=template.get("routing",{}).get("rules",[])
for route in routes:
    if route["routing"]["outbound_tag"] in outbounds: raise SystemExit("managed outbound still present after removal")
    if any(rule.get("ruleTag")==route["routing"]["rule_tag"] for rule in rules): raise SystemExit("managed routing rule still present after removal")
PY
}

wm_route_set() {
  local operation="$1"; shift
  local route_id="" transaction candidate affected inbound_id old_enabled inbound_file outbound_file reconciled_id updated_candidate
  while [[ $# -gt 0 ]]; do case "$1" in --route-id) route_id="${2:-}"; shift 2;; *) wm_fail "Unknown route option: $1";; esac; done
  [[ -n "$route_id" ]] || wm_fail "Usage: wavemesh route ${operation} --route-id ID"
  wm_lock_mutation "route-${operation}"; wm_load_config; wm_require_entry_role; wm_transaction_begin "route-${operation}"; transaction="$WM_ACTIVE_TRANSACTION"; candidate="$transaction/config.candidate.json"; affected="$transaction/affected.json"
  python3 "$WM_RUNTIME_TOOL" mutate --config "$WM_CONFIG_JSON" --output "$candidate" --operation "$operation" --target "$route_id" --affected "$affected" || wm_fail "Could not build route mutation"
  read -r inbound_id old_enabled < <(python3 - "$WM_CONFIG_JSON" "$route_id" <<'PY'
import json,sys
r=next(x for x in json.load(open(sys.argv[1],encoding="utf-8"))["routes"] if x["id"]==sys.argv[2]); print(r["entry"]["inbound_id"],str(r.get("enabled",True)).lower())
PY
)
  inbound_file="$transaction/inbound.json"; outbound_file="$transaction/outbound.json"
  if [[ "$operation" == "enable" ]]; then
    python3 "$WM_RUNTIME_TOOL" artifacts --config "$candidate" --route-id "$route_id" --inbound "$inbound_file" --outbound "$outbound_file" || wm_fail "Could not render route artifacts"
    reconciled_id="$(wm_inbound_reconcile "$inbound_file")" || wm_fail "Could not reconcile route inbound"
    wm_inbound_set_enabled "$reconciled_id" true || wm_fail "Could not enable route inbound"
    updated_candidate="$transaction/config.inbound.json"
    python3 "$WM_RUNTIME_TOOL" set-inbound-id --config "$candidate" --output "$updated_candidate" --route-id "$route_id" --inbound-id "$reconciled_id"
    candidate="$updated_candidate"
    wm_xray_apply_managed_route "$outbound_file" "$(python3 -c 'import json,sys; r=next(x for x in json.load(open(sys.argv[1]))["routes"] if x["id"]==sys.argv[2]); print(r["entry"]["inbound_tag"])' "$candidate" "$route_id")" "$(python3 -c 'import json,sys; r=next(x for x in json.load(open(sys.argv[1]))["routes"] if x["id"]==sys.argv[2]); print(r["routing"]["outbound_tag"])' "$candidate" "$route_id")" "$(python3 -c 'import json,sys; r=next(x for x in json.load(open(sys.argv[1]))["routes"] if x["id"]==sys.argv[2]); print(r["routing"]["rule_tag"])' "$candidate" "$route_id")" || wm_fail "Could not verify managed route"
  else
    wm_inbound_set_enabled "$inbound_id" false || wm_fail "Could not disable route inbound"
  fi
  wm_runtime_prepare_subscriptions "$candidate" "$transaction" || wm_fail "Could not render subscriptions"
  wm_runtime_apply_public_state "$transaction" || wm_fail "Could not apply public route state; transaction will be rolled back"
  wm_atomic_install_json "$WM_RUNTIME_CANDIDATE" "$WM_CONFIG_JSON"; wm_export_config_env_from_json
  python3 "$WM_RUNTIME_TOOL" sync --config "$WM_CONFIG_JSON" --runtime "$WM_RUNTIME_JSON" --output "$WM_RUNTIME_JSON"
  wm_transaction_commit "$transaction"
  wm_success "Route ${operation}d: ${route_id}"
}

wm_route_remove() {
  local route_id="" transaction candidate affected inbound_id original_xray candidate_xray
  while [[ $# -gt 0 ]]; do case "$1" in --route-id) route_id="${2:-}"; shift 2;; *) wm_fail "Unknown route remove option: $1";; esac; done
  [[ -n "$route_id" ]] || wm_fail "Usage: wavemesh route remove --route-id ID"
  wm_lock_mutation "route-remove"; wm_load_config; wm_require_entry_role; wm_transaction_begin "route-remove"; transaction="$WM_ACTIVE_TRANSACTION"; candidate="$transaction/config.candidate.json"; affected="$transaction/affected.json"
  python3 "$WM_RUNTIME_TOOL" mutate --config "$WM_CONFIG_JSON" --output "$candidate" --operation remove-route --target "$route_id" --affected "$affected" || wm_fail "Could not build route removal"
  inbound_id="$(python3 -c 'import json,sys; r=next(x for x in json.load(open(sys.argv[1]))["routes"] if x["id"]==sys.argv[2]); print(r["entry"]["inbound_id"])' "$WM_CONFIG_JSON" "$route_id")"
  wm_runtime_prepare_subscriptions "$candidate" "$transaction" || wm_fail "Could not render route removal"
  wm_runtime_apply_public_state "$transaction" || wm_fail "Could not remove public route state; transaction will be rolled back"
  original_xray="$transaction/xray.before.json"; candidate_xray="$transaction/xray.candidate.json"
  wm_runtime_remove_xray_routes "$WM_CONFIG_JSON" "$affected" "$original_xray" "$candidate_xray" || wm_fail "Could not remove Xray route; transaction will be rolled back"
  wm_inbound_delete "$inbound_id" || wm_fail "Could not delete route inbound; transaction will be rolled back"
  wm_atomic_install_json "$WM_RUNTIME_CANDIDATE" "$WM_CONFIG_JSON"; wm_export_config_env_from_json
  python3 "$WM_RUNTIME_TOOL" sync --config "$WM_CONFIG_JSON" --runtime "$WM_RUNTIME_JSON" --output "$WM_RUNTIME_JSON"
  wm_transaction_commit "$transaction"
  wm_success "Route removed: ${route_id}"
}

wm_route_command() {
  case "${1:-}" in
    list) shift; wm_runtime_status "$@";;
    enable) shift; wm_route_set enable "$@";;
    disable) shift; wm_route_set disable "$@";;
    remove) shift; wm_route_remove "$@";;
    *) wm_fail "Usage: wavemesh route list [--json] | enable|disable|remove --route-id ID";;
  esac
}

wm_cascade_remove_exit() {
  local exit_id="" force=0 transaction candidate affected original_xray candidate_xray inbound_id
  while [[ $# -gt 0 ]]; do case "$1" in --exit-id) exit_id="${2:-}"; shift 2;; --force) force=1; shift;; *) wm_fail "Unknown remove-exit option: $1";; esac; done
  [[ -n "$exit_id" ]] || wm_fail "Usage: wavemesh cascade remove-exit --exit-id ID [--force]"
  wm_lock_mutation "cascade-remove-exit"; wm_load_config; wm_require_entry_role; wm_transaction_begin "cascade-remove-exit"; transaction="$WM_ACTIVE_TRANSACTION"; candidate="$transaction/config.candidate.json"; affected="$transaction/affected.json"
  local args=(mutate --config "$WM_CONFIG_JSON" --output "$candidate" --operation remove-exit --target "$exit_id" --affected "$affected"); (( force == 1 )) && args+=(--force)
  python3 "$WM_RUNTIME_TOOL" "${args[@]}" || wm_fail "Could not build Exit removal"
  wm_runtime_prepare_subscriptions "$candidate" "$transaction" || wm_fail "Could not render Exit removal"
  wm_runtime_apply_public_state "$transaction" || wm_fail "Could not remove Exit public state; transaction will be rolled back"
  original_xray="$transaction/xray.before.json"; candidate_xray="$transaction/xray.candidate.json"
  wm_runtime_remove_xray_routes "$WM_CONFIG_JSON" "$affected" "$original_xray" "$candidate_xray" || wm_fail "Could not remove Exit routing; transaction will be rolled back"
  while IFS= read -r inbound_id; do
    [[ -n "$inbound_id" ]] || continue
    wm_inbound_delete "$inbound_id" || wm_fail "Could not delete all route inbounds; transaction will be rolled back"
  done < <(python3 - "$WM_CONFIG_JSON" "$affected" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1],encoding="utf-8")); ids=set(json.load(open(sys.argv[2],encoding="utf-8")))
for r in cfg.get("routes",[]):
    if r.get("id") in ids: print(r["entry"]["inbound_id"])
PY
)
  wm_atomic_install_json "$WM_RUNTIME_CANDIDATE" "$WM_CONFIG_JSON"; wm_export_config_env_from_json
  python3 "$WM_RUNTIME_TOOL" sync --config "$WM_CONFIG_JSON" --runtime "$WM_RUNTIME_JSON" --output "$WM_RUNTIME_JSON"
  wm_transaction_commit "$transaction"
  wm_success "Exit removed: ${exit_id}"
}

wm_reconcile_command() {
  local mode="${1:---dry-run}" transaction candidate route_id enabled inbound_file outbound_file inbound_tag outbound_tag rule_tag reconciled_id updated_candidate
  [[ "$mode" == "--dry-run" || "$mode" == "--apply" ]] || wm_fail "Usage: wavemesh reconcile --dry-run|--apply"
  wm_runtime_health --json >/dev/null
  if [[ "$mode" == "--dry-run" ]]; then python3 "$WM_RUNTIME_TOOL" plan --config "$WM_CONFIG_JSON" --runtime "$WM_RUNTIME_JSON"; return; fi
  wm_lock_mutation "reconcile-apply"; wm_load_config; wm_require_entry_role
  wm_apply_subscription_presentation || wm_fail "Could not apply subscription presentation settings"
  wm_transaction_begin "reconcile-apply"; transaction="$WM_ACTIVE_TRANSACTION"; candidate="$transaction/config.candidate.json"; cp "$WM_CONFIG_JSON" "$candidate"
  while IFS=$'\t' read -r route_id enabled inbound_tag outbound_tag rule_tag; do
    inbound_file="$transaction/${route_id}.inbound.json"; outbound_file="$transaction/${route_id}.outbound.json"
    python3 "$WM_RUNTIME_TOOL" artifacts --config "$candidate" --route-id "$route_id" --inbound "$inbound_file" --outbound "$outbound_file" || wm_fail "Could not build reconcile artifacts"
    if [[ "$enabled" == "true" ]]; then
      reconciled_id="$(wm_inbound_reconcile "$inbound_file")" || wm_fail "Could not reconcile inbound for ${route_id}"
      wm_inbound_set_enabled "$reconciled_id" true || wm_fail "Could not enable reconciled inbound for ${route_id}"
      wm_xray_apply_managed_route "$outbound_file" "$inbound_tag" "$outbound_tag" "$rule_tag" || wm_fail "Could not reconcile Xray for ${route_id}"
    else
      reconciled_id="$(wm_inbound_reconcile "$inbound_file")" || wm_fail "Could not reconcile disabled inbound for ${route_id}"
      wm_inbound_set_enabled "$reconciled_id" false || wm_fail "Could not keep disabled inbound ${route_id} disabled"
    fi
    updated_candidate="$transaction/config.${route_id}.json"
    python3 "$WM_RUNTIME_TOOL" set-inbound-id --config "$candidate" --output "$updated_candidate" --route-id "$route_id" --inbound-id "$reconciled_id"
    candidate="$updated_candidate"
  done < <(python3 - "$WM_CONFIG_JSON" <<'PY'
import json,sys
for r in json.load(open(sys.argv[1],encoding="utf-8")).get("routes",[]):
    if r.get("kind")=="cascade": print("\t".join((r["id"],str(r.get("enabled",True)).lower(),r["entry"]["inbound_tag"],r["routing"]["outbound_tag"],r["routing"]["rule_tag"])))
PY
)
  wm_runtime_prepare_subscriptions "$candidate" "$transaction" || wm_fail "Could not rebuild subscriptions"
  wm_runtime_apply_public_state "$transaction" || wm_fail "Could not reconcile nginx/subscriptions; transaction will be rolled back"
  wm_atomic_install_json "$WM_RUNTIME_CANDIDATE" "$WM_CONFIG_JSON"; wm_export_config_env_from_json
  wm_transaction_commit "$transaction"
  wm_runtime_health
}
