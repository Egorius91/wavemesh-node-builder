#!/usr/bin/env bash

WM_SUBSCRIPTION_PATH_TOOL="${WM_SUBSCRIPTION_PATH_TOOL:-$WM_LIB_DIR/lib/subscription_path.py}"

wm_subscription_rebuild_command() {
  local transaction candidate prepared metadata backup
  wm_lock_mutation "subscription-rebuild"; wm_load_config
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
  wm_load_config; transaction="$(mktemp -d)"; candidate="$transaction/config.json"; prepared="$transaction/subscriptions"; metadata="$transaction/metadata.json"; mkdir -p "$prepared"
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

wm_subscription_command() {
  case "${1:-}" in
    rebuild) shift; wm_subscription_rebuild_command "$@" ;;
    validate) shift; wm_subscription_validate_command "$@" ;;
    rotate-path) shift; wm_subscription_rotate_path_command "$@" ;;
    *) wm_fail "Usage: wavemesh subscription rebuild|validate|rotate-path --dry-run|--apply [--path PATH]" ;;
  esac
}
