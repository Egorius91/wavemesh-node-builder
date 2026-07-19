#!/usr/bin/env bash

WM_SUBSCRIPTION_PATH_TOOL="${WM_SUBSCRIPTION_PATH_TOOL:-$WM_LIB_DIR/lib/subscription_path.py}"
WM_ROUTE_PRESENTATION_TOOL="${WM_ROUTE_PRESENTATION_TOOL:-$WM_LIB_DIR/lib/route_presentation.py}"

wm_subscription_native_rebuild_command() {
  local transaction
  wm_transaction_begin "subscription-native-rebuild"; transaction="$WM_ACTIVE_TRANSACTION"
  wm_native_apply_settings "$WM_CONFIG_JSON" || wm_fail "Could not apply native subscription settings"
  wm_nginx_apply_desired "$WM_CONFIG_JSON" "$transaction" || wm_fail "nginx rejected native subscription proxy"
  wm_native_validate_public "$WM_CONFIG_JSON" || wm_fail "Native public subscription validation failed"
  wm_transaction_commit "$transaction"
  wm_success "3X-UI native subscription backend rebuilt and validated"
}

wm_subscription_native_validate_command() {
  wm_native_validate_public "$WM_CONFIG_JSON" || wm_fail "Native subscription validation failed"
  wm_success "3X-UI native subscription backend and public output are valid"
}

wm_subscription_rebuild_command() {
  local transaction candidate prepared metadata backup
  wm_lock_mutation "subscription-rebuild"; wm_load_config
  if [[ "$(wm_subscription_backend "$WM_CONFIG_JSON")" == "xui-native" ]]; then
    wm_subscription_native_rebuild_command
    return
  fi
  wm_apply_subscription_presentation || wm_fail "Could not apply subscription presentation settings"
  wm_transaction_begin "subscription-rebuild"; transaction="$WM_ACTIVE_TRANSACTION"; candidate="$transaction/config.candidate.json"; prepared="$transaction/subscriptions"; metadata="$transaction/subscriptions.json"; backup="$transaction/subscriptions.before"
  mkdir -p "$prepared" "$backup"
  wm_subscription_prepare "$WM_CONFIG_JSON" "$candidate" "$prepared" "$metadata" || wm_fail "Could not render subscriptions"
  wm_subscription_install_files "$prepared" "$backup"
  wm_nginx_apply_desired "$candidate" "$transaction" || wm_fail "nginx rejected subscription locations; transaction will be rolled back"
  wm_subscription_validate_public "$metadata" || wm_fail "Public subscription validation failed; transaction will be rolled back"
  wm_atomic_install_json "$candidate" "$WM_CONFIG_JSON"; wm_export_config_env_from_json; wm_transaction_commit "$transaction"
  python3 - "$metadata" "$DOMAIN" <<'PY'
import json,sys
for item in json.load(open(sys.argv[1],encoding="utf-8")):
    print(f"{item['client_id']}\t{item['profiles']} profile(s)\thttps://{sys.argv[2]}{item['path']}")
PY
}

wm_subscription_validate_command() {
  local transaction candidate prepared metadata item sub_id installed generated
  wm_load_config
  if [[ "$(wm_subscription_backend "$WM_CONFIG_JSON")" == "xui-native" ]]; then
    wm_subscription_native_validate_command
    return
  fi
  transaction="$(mktemp -d)"; candidate="$transaction/config.json"; prepared="$transaction/subscriptions"; metadata="$transaction/metadata.json"; mkdir -p "$prepared"
  wm_subscription_prepare "$WM_CONFIG_JSON" "$candidate" "$prepared" "$metadata" || wm_fail "Could not render desired subscriptions"
  while IFS= read -r item; do
    sub_id="$(printf '%s' "$item" | python3 -c 'import json,sys; print(json.load(sys.stdin)["subscription_id"])')"; installed="$WM_SUB_DIR/users/${sub_id}.txt"; generated="$prepared/users/${sub_id}.txt"
    [[ -f "$installed" ]] || wm_fail "Subscription file is missing for a configured client; run wavemesh subscription rebuild"
    cmp -s "$installed" "$generated" || wm_fail "Subscription file differs from desired state; run wavemesh subscription rebuild"
  done < <(python3 -c 'import json,sys; [print(json.dumps(x,separators=(",",":"))) for x in json.load(open(sys.argv[1]))]' "$metadata")
  wm_subscription_validate_public "$metadata" || wm_fail "Public subscription validation failed"
  wm_success "Subscriptions match desired state and public output"
}

wm_subscription_rotate_path_command() {
  local mode="" requested="" transaction raw_candidate candidate prepared metadata backup plan old_path new_path
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) mode="dry-run"; shift ;;
      --apply) mode="apply"; shift ;;
      --path) requested="${2:-}"; shift 2 ;;
      *) wm_fail "Unknown rotate-path option: $1" ;;
    esac
  done
  [[ -n "$mode" ]] || wm_fail "Usage: wavemesh subscription rotate-path --dry-run|--apply [--path /opaque/path/]"
  wm_lock_mutation "subscription-rotate-path"; wm_load_config
  transaction="$(mktemp -d)"; chmod 700 "$transaction"; raw_candidate="$transaction/config.path.json"
  local args=(--config "$WM_CONFIG_JSON" --output "$raw_candidate")
  [[ -n "$requested" ]] && args+=(--path "$requested")
  plan="$(python3 "$WM_SUBSCRIPTION_PATH_TOOL" "${args[@]}")" || wm_fail "Could not build subscription path rotation"
  old_path="$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["old_path"])')"
  new_path="$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["new_path"])')"
  wm_info "Current subscription path: ${old_path}"
  wm_info "New subscription path: ${new_path}"
  if [[ "$mode" == "dry-run" ]]; then
    rm -rf "$transaction"
    wm_info "Dry run only; no files, nginx locations, or client links were changed"
    return 0
  fi
  if [[ "$(wm_subscription_backend "$WM_CONFIG_JSON")" == "xui-native" ]]; then
    rm -rf "$transaction"
    wm_transaction_begin "subscription-native-rotate-path"; transaction="$WM_ACTIVE_TRANSACTION"
    raw_candidate="$transaction/config.path.json"
    args=(--config "$WM_CONFIG_JSON" --output "$raw_candidate")
    [[ -n "$requested" ]] && args+=(--path "$requested")
    python3 "$WM_SUBSCRIPTION_PATH_TOOL" "${args[@]}" >/dev/null || wm_fail "Could not build native subscription path rotation"
    wm_native_apply_settings "$raw_candidate" || wm_fail "Could not apply rotated native settings"
    wm_nginx_apply_native_rotation_candidate "$raw_candidate" "$transaction" "$old_path" "$new_path" || wm_fail "nginx rejected two-phase native subscription rotation"
    wm_native_validate_public "$raw_candidate" || wm_fail "Rotated native public subscription validation failed"
    wm_nginx_apply_desired "$raw_candidate" "$transaction" || wm_fail "nginx could not remove the old native subscription path"
    wm_native_validate_public "$raw_candidate" || wm_fail "Native subscription failed after removal of the old path"
    wm_atomic_install_json "$raw_candidate" "$WM_CONFIG_JSON"; wm_export_config_env_from_json
    wm_transaction_commit "$transaction"
    wm_success "Native subscription path rotated and old public path removed"
    return
  fi
  rm -rf "$transaction"
  wm_transaction_begin "subscription-rotate-path"; transaction="$WM_ACTIVE_TRANSACTION"
  raw_candidate="$transaction/config.path.json"; candidate="$transaction/config.candidate.json"; prepared="$transaction/subscriptions"; metadata="$transaction/subscriptions.json"; backup="$transaction/subscriptions.before"
  mkdir -p "$prepared" "$backup"
  args=(--config "$WM_CONFIG_JSON" --output "$raw_candidate")
  [[ -n "$requested" ]] && args+=(--path "$requested")
  plan="$(python3 "$WM_SUBSCRIPTION_PATH_TOOL" "${args[@]}")" || wm_fail "Could not build subscription path rotation"
  wm_subscription_prepare "$raw_candidate" "$candidate" "$prepared" "$metadata" || wm_fail "Could not render rotated subscriptions"
  wm_subscription_install_files "$prepared" "$backup"
  wm_nginx_apply_desired "$candidate" "$transaction" || wm_fail "nginx rejected rotated subscription locations; transaction will be rolled back"
  wm_subscription_validate_public "$metadata" || wm_fail "Rotated public subscription validation failed; transaction will be rolled back"
  wm_atomic_install_json "$candidate" "$WM_CONFIG_JSON"; wm_export_config_env_from_json; wm_transaction_commit "$transaction"
  wm_success "Subscription path rotated. Update the bot and clients to the new URL before the next refresh"
  python3 - "$metadata" "$DOMAIN" <<'PY'
import json,sys
for item in json.load(open(sys.argv[1],encoding="utf-8")):
    print(f"{item['client_id']}\thttps://{sys.argv[2]}{item['path']}")
PY
}

wm_subscription_capabilities_command() {
  [[ "${1:-}" == "--json" ]] || wm_fail "Usage: wavemesh subscription capabilities --json"
  wm_load_config
  wm_native_capabilities_json "$WM_CONFIG_JSON"
}

wm_subscription_migrate_native_command() {
  local mode="" transaction path_candidate candidate reconciled
  while [[ $# -gt 0 ]]; do case "$1" in --dry-run) mode="dry-run"; shift;; --apply) mode="apply"; shift;; *) wm_fail "Usage: wavemesh subscription migrate-native --dry-run|--apply";; esac; done
  [[ -n "$mode" ]] || wm_fail "Usage: wavemesh subscription migrate-native --dry-run|--apply"
  wm_lock_mutation "subscription-migrate-native"; wm_load_config
  [[ "$(wm_subscription_backend "$WM_CONFIG_JSON")" != "xui-native" ]] || wm_fail "Subscription backend is already xui-native"
  transaction="$(mktemp -d)"; chmod 700 "$transaction"; path_candidate="$transaction/config.path.json"; candidate="$transaction/config.native.json"
  python3 "$WM_SUBSCRIPTION_PATH_TOOL" --config "$WM_CONFIG_JSON" --output "$path_candidate" >/dev/null || wm_fail "Could not generate an opaque native subscription path"
  python3 "$WM_NATIVE_SUBSCRIPTION_TOOL" set-backend --config "$path_candidate" --output "$candidate" --backend xui-native
  wm_info "Migration target: generated -> xui-native"
  wm_info "A new opaque public subscription path will replace the generated renderer locations"
  if [[ "$mode" == "dry-run" ]]; then
    local clients_plan actions_plan reconciled_plan action_count
    clients_plan="$transaction/clients.json"; actions_plan="$transaction/actions.json"; reconciled_plan="$transaction/config.reconciled.json"
    wm_xui_request_success GET /panel/api/clients/list none > "$clients_plan" || wm_fail "Clients API is unavailable"
    python3 "$WM_NATIVE_SUBSCRIPTION_TOOL" client-plan --config "$candidate" --clients "$clients_plan" --output-config "$reconciled_plan" --actions "$actions_plan" || wm_fail "Builder clients cannot be mapped safely to native clients"
    action_count="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$actions_plan")"
    wm_info "Builder client subId updates required: ${action_count}"
    if (( action_count == 0 )); then
      wm_native_validate_profile_counts "$reconciled_plan" || wm_fail "Clients API profiles do not match canonical published routes"
      wm_success "Clients API profile counts match canonical published routes"
    else
      wm_warn "Profile count validation is deferred until missing subId values are applied transactionally"
    fi
    wm_native_capabilities_json "$WM_CONFIG_JSON"
    rm -rf "$transaction"
    return
  fi
  rm -rf "$transaction"
  wm_transaction_begin "subscription-migrate-native"; transaction="$WM_ACTIVE_TRANSACTION"; path_candidate="$transaction/config.path.json"; candidate="$transaction/config.native.json"
  python3 "$WM_SUBSCRIPTION_PATH_TOOL" --config "$WM_CONFIG_JSON" --output "$path_candidate" >/dev/null || wm_fail "Could not generate native subscription path"
  python3 "$WM_NATIVE_SUBSCRIPTION_TOOL" set-backend --config "$path_candidate" --output "$candidate" --backend xui-native
  reconciled="$transaction/config.native.clients.json"
  wm_native_reconcile_builder_subids "$candidate" "$reconciled" || wm_fail "Could not preserve or establish native subscription IDs for builder clients"
  candidate="$reconciled"
  wm_native_apply_settings "$candidate" || wm_fail "Could not apply native 3X-UI settings"
  wm_nginx_apply_native_migration_candidate "$WM_CONFIG_JSON" "$candidate" "$transaction" || wm_fail "nginx rejected generated/native coexistence configuration"
  wm_native_validate_public "$candidate" true || wm_fail "Native public subscription validation failed; transaction will roll back"
  wm_nginx_apply_desired "$candidate" "$transaction" || wm_fail "nginx could not disable generated renderer locations"
  wm_native_validate_public "$candidate" || wm_fail "Native subscription failed after generated renderer removal"
  wm_atomic_install_json "$candidate" "$WM_CONFIG_JSON"; wm_export_config_env_from_json
  wm_transaction_commit "$transaction"
  wm_success "Subscription backend migrated to xui-native"
}

wm_subscription_fallback_generated_command() {
  local mode="" transaction candidate rendered metadata prepared backup
  while [[ $# -gt 0 ]]; do case "$1" in --dry-run) mode="dry-run"; shift;; --apply) mode="apply"; shift;; *) wm_fail "Usage: wavemesh subscription fallback-generated --dry-run|--apply";; esac; done
  [[ -n "$mode" ]] || wm_fail "Usage: wavemesh subscription fallback-generated --dry-run|--apply"
  wm_lock_mutation "subscription-fallback-generated"; wm_load_config
  [[ "$(wm_subscription_backend "$WM_CONFIG_JSON")" == "xui-native" ]] || wm_fail "Subscription backend is already generated"
  transaction="$(mktemp -d)"; candidate="$transaction/config.generated.json"; rendered="$transaction/config.rendered.json"; prepared="$transaction/subscriptions"; metadata="$transaction/metadata.json"; mkdir -p "$prepared"
  python3 "$WM_NATIVE_SUBSCRIPTION_TOOL" set-backend --config "$WM_CONFIG_JSON" --output "$candidate" --backend generated
  wm_subscription_prepare "$candidate" "$rendered" "$prepared" "$metadata" || wm_fail "Could not render generated fallback subscriptions"
  if [[ "$mode" == "dry-run" ]]; then rm -rf "$transaction"; wm_info "Generated fallback can be rendered; no state changed"; return; fi
  rm -rf "$transaction"; wm_transaction_begin "subscription-fallback-generated"; transaction="$WM_ACTIVE_TRANSACTION"; candidate="$transaction/config.generated.json"; rendered="$transaction/config.rendered.json"; prepared="$transaction/subscriptions"; metadata="$transaction/metadata.json"; backup="$transaction/subscriptions.before"; mkdir -p "$prepared" "$backup"
  python3 "$WM_NATIVE_SUBSCRIPTION_TOOL" set-backend --config "$WM_CONFIG_JSON" --output "$candidate" --backend generated
  wm_subscription_prepare "$candidate" "$rendered" "$prepared" "$metadata" || wm_fail "Could not render generated fallback subscriptions"
  wm_subscription_install_files "$prepared" "$backup"
  wm_nginx_apply_desired "$rendered" "$transaction" || wm_fail "nginx rejected generated fallback locations"
  wm_subscription_validate_public "$metadata" || wm_fail "Generated fallback public validation failed"
  wm_atomic_install_json "$rendered" "$WM_CONFIG_JSON"; wm_export_config_env_from_json; wm_transaction_commit "$transaction"
  wm_success "Subscription backend switched to generated fallback"
}


wm_subscription_publication_mode_command() {
  local requested="" action="" as_json=0 current_summary candidate_summary transaction candidate inbounds preflight ready
  while [[ $# -gt 0 ]]; do
    case "$1" in
      all|auto-only) [[ -z "$requested" ]] || wm_fail "Publication mode was specified more than once"; requested="$1"; shift ;;
      --dry-run) [[ -z "$action" ]] || wm_fail "Use only one of --dry-run or --apply"; action="dry-run"; shift ;;
      --apply) [[ -z "$action" ]] || wm_fail "Use only one of --dry-run or --apply"; action="apply"; shift ;;
      --json) as_json=1; shift ;;
      *) wm_fail "Usage: wavemesh subscription publication-mode [--json] | all|auto-only --dry-run|--apply [--json]" ;;
    esac
  done

  wm_load_config
  wm_require_entry_role
  current_summary="$(python3 "$WM_ROUTE_PRESENTATION_TOOL" status --config "$WM_CONFIG_JSON")" || wm_fail "Could not read subscription publication mode"
  if [[ -z "$requested" ]]; then
    if (( as_json == 1 )); then
      printf '%s\n' "$current_summary" | python3 -m json.tool
    else
      printf '%s\n' "$current_summary" | python3 -c 'import json,sys; data=json.load(sys.stdin); print("Publication mode: {}".format(data["mode"])); print("Public routes: {}".format(data["public_route_count"])); print("Hidden enabled routes: {}".format(len(data["hidden_enabled_route_ids"])))'
    fi
    return
  fi
  [[ -n "$action" ]] || wm_fail "Use --dry-run or --apply when changing publication mode"

  wm_lock_mutation "subscription-publication-mode"
  transaction="$(mktemp -d)"; chmod 700 "$transaction"
  candidate="$transaction/config.publication.json"
  candidate_summary="$(python3 "$WM_ROUTE_PRESENTATION_TOOL" set --config "$WM_CONFIG_JSON" --output "$candidate" --mode "$requested")" || { rm -rf "$transaction"; wm_fail "Could not build publication mode candidate"; }

  preflight='{"ready":true,"source_profile_count":0,"auto_target_count":0,"targets":[]}'
  if [[ "$requested" == "auto-only" && "$(wm_subscription_backend "$WM_CONFIG_JSON")" == "xui-native" ]]; then
    inbounds="$transaction/inbounds.json"
    wm_xui_request_success GET /panel/api/inbounds/list none > "$inbounds" || { rm -rf "$transaction"; wm_fail "Could not inspect 3X-UI inbounds for Auto-only preflight"; }
    preflight="$(python3 "$WM_ROUTE_PRESENTATION_TOOL" native-preflight --current "$WM_CONFIG_JSON" --candidate "$candidate" --inbounds "$inbounds")" || { rm -rf "$transaction"; wm_fail "Could not evaluate native Auto-only readiness"; }
    ready="$(printf '%s\n' "$preflight" | python3 -c 'import json,sys; print(str(json.load(sys.stdin)["ready"]).lower())')"
    if [[ "$ready" != "true" ]]; then
      if (( as_json == 1 )); then
        CANDIDATE="$candidate_summary" PREFLIGHT="$preflight" python3 - <<'PYREPORT'
import json,os
print(json.dumps({"candidate":json.loads(os.environ["CANDIDATE"]),"native_preflight":json.loads(os.environ["PREFLIGHT"])},indent=2,ensure_ascii=False,sort_keys=True))
PYREPORT
      fi
      rm -rf "$transaction"
      wm_fail "Auto-only preflight found active subscription clients missing from the published Auto inbound; synchronize bot keys before applying"
    fi
  fi

  if [[ "$action" == "dry-run" ]]; then
    if (( as_json == 1 )); then
      CANDIDATE="$candidate_summary" PREFLIGHT="$preflight" python3 - <<'PYREPORT'
import json,os
print(json.dumps({"candidate":json.loads(os.environ["CANDIDATE"]),"native_preflight":json.loads(os.environ["PREFLIGHT"])},indent=2,ensure_ascii=False,sort_keys=True))
PYREPORT
    else
      CANDIDATE="$candidate_summary" PREFLIGHT="$preflight" python3 - <<'PYREPORT'
import json,os
candidate=json.loads(os.environ["CANDIDATE"]); preflight=json.loads(os.environ["PREFLIGHT"])
print("Target publication mode: {}".format(candidate["mode"]))
print("Public routes after apply: {}".format(candidate["public_route_count"]))
print("Hidden enabled routes after apply: {}".format(len(candidate["hidden_enabled_route_ids"])))
if candidate["mode"] == "auto-only":
    print("Native source profiles checked: {}".format(preflight["source_profile_count"]))
    print("Published Auto targets: {}".format(preflight["auto_target_count"]))
PYREPORT
    fi
    rm -rf "$transaction"
    wm_success "Publication mode dry-run passed; no state changed"
    return
  fi

  rm -rf "$transaction"
  wm_transaction_begin "subscription-publication-mode"; transaction="$WM_ACTIVE_TRANSACTION"
  candidate="$transaction/config.publication.json"
  python3 "$WM_ROUTE_PRESENTATION_TOOL" set --config "$WM_CONFIG_JSON" --output "$candidate" --mode "$requested" >/dev/null || wm_fail "Could not rebuild publication mode candidate"
  if [[ "$requested" == "auto-only" && "$(wm_subscription_backend "$WM_CONFIG_JSON")" == "xui-native" ]]; then
    inbounds="$transaction/inbounds.preflight.json"
    wm_xui_request_success GET /panel/api/inbounds/list none > "$inbounds" || wm_fail "Could not repeat 3X-UI Auto-only preflight inside transaction"
    preflight="$(python3 "$WM_ROUTE_PRESENTATION_TOOL" native-preflight --current "$WM_CONFIG_JSON" --candidate "$candidate" --inbounds "$inbounds")" || wm_fail "Could not repeat native Auto-only readiness check"
    ready="$(printf '%s\n' "$preflight" | python3 -c 'import json,sys; print(str(json.load(sys.stdin)["ready"]).lower())')"
    [[ "$ready" == "true" ]] || wm_fail "Auto-only readiness changed after dry-run; no state was committed"
  fi

  local route_id kind enabled desired outbound reconciled_id updated_candidate
  while IFS=$'\t' read -r route_id kind enabled; do
    [[ -n "$route_id" ]] || continue
    desired="$transaction/${route_id}.inbound.json"
    if [[ "$kind" == "auto" ]]; then
      python3 "$WM_RUNTIME_TOOL" inbound-artifact --config "$candidate" --route-id "$route_id" --output "$desired" || wm_fail "Could not build Auto inbound presentation for ${route_id}"
    else
      outbound="$transaction/${route_id}.outbound.json"
      python3 "$WM_RUNTIME_TOOL" artifacts --config "$candidate" --route-id "$route_id" --inbound "$desired" --outbound "$outbound" || wm_fail "Could not build route presentation for ${route_id}"
    fi
    reconciled_id="$(wm_inbound_reconcile "$desired")" || wm_fail "Could not reconcile subscription presentation for ${route_id}"
    wm_inbound_set_enabled "$reconciled_id" "$enabled" || wm_fail "Could not preserve inbound enabled state for ${route_id}"
    updated_candidate="$transaction/config.${route_id}.json"
    python3 "$WM_RUNTIME_TOOL" set-inbound-id --config "$candidate" --output "$updated_candidate" --route-id "$route_id" --inbound-id "$reconciled_id" || wm_fail "Could not preserve inbound identity for ${route_id}"
    candidate="$updated_candidate"
  done < <(python3 - "$candidate" <<'PYROUTES'
import json,sys
cfg=json.load(open(sys.argv[1],encoding="utf-8"))
routes=sorted((r for r in cfg.get("routes",[]) if r.get("kind") in ("auto","cascade")),key=lambda r:(0 if r.get("kind")=="auto" else 1,r.get("sort_order",0),r.get("id","")))
for route in routes:
    print("\t".join((route["id"],route["kind"],str(route.get("enabled",True)).lower())))
PYROUTES
  )

  wm_runtime_prepare_subscriptions "$candidate" "$transaction" || wm_fail "Could not prepare publication-mode subscriptions"
  wm_runtime_apply_public_state "$transaction" || wm_fail "Publication-mode public output failed validation"
  wm_atomic_install_json "$WM_RUNTIME_CANDIDATE" "$WM_CONFIG_JSON"; wm_export_config_env_from_json
  python3 "$WM_RUNTIME_TOOL" sync --config "$WM_CONFIG_JSON" --runtime "$WM_RUNTIME_JSON" --output "$WM_RUNTIME_JSON"
  wm_transaction_commit "$transaction"
  wm_success "Subscription publication mode applied: ${requested}"
}

wm_subscription_command() {
  case "${1:-}" in
    rebuild) shift; wm_subscription_rebuild_command "$@" ;;
    validate) shift; wm_subscription_validate_command "$@" ;;
    rotate-path) shift; wm_subscription_rotate_path_command "$@" ;;
    capabilities) shift; wm_subscription_capabilities_command "$@" ;;
    migrate-native) shift; wm_subscription_migrate_native_command "$@" ;;
    fallback-generated) shift; wm_subscription_fallback_generated_command "$@" ;;
    publication-mode) shift; wm_subscription_publication_mode_command "$@" ;;
    *) wm_fail "Usage: wavemesh subscription rebuild|validate|capabilities --json|migrate-native|fallback-generated --dry-run|--apply|rotate-path --dry-run|--apply [--path PATH]|publication-mode [--json]|all|auto-only --dry-run|--apply" ;;
  esac
}
