#!/usr/bin/env bash

declare -F wm_lock_mutation >/dev/null || wm_lock_mutation() { mkdir -p /run/lock; exec 9>/run/lock/wavemesh-node.lock; flock -n 9 || wm_fail "Another WaveMesh mutation is running"; }
declare -F wm_transaction_dir >/dev/null || wm_transaction_dir() { local dir="$WM_STATE_DIR/transactions/$(date -u +%Y%m%dT%H%M%SZ)-$(wm_random_alnum 6)"; mkdir -p "$dir"; chmod 700 "$WM_STATE_DIR/transactions" "$dir"; printf '%s' "$dir"; }

wm_subscription_rebuild_command() {
  local transaction candidate prepared metadata backup
  wm_lock_mutation; wm_load_config; transaction="$(wm_transaction_dir)"; candidate="$transaction/config.candidate.json"; prepared="$transaction/subscriptions"; metadata="$transaction/subscriptions.json"; backup="$transaction/subscriptions.before"
  cp "$WM_CONFIG_JSON" "$transaction/config.before.json"; mkdir -p "$prepared" "$backup"
  wm_subscription_prepare "$WM_CONFIG_JSON" "$candidate" "$prepared" "$metadata" || wm_fail "Could not render subscriptions"
  wm_subscription_install_files "$prepared" "$backup"
  if ! wm_nginx_apply_desired "$candidate" "$transaction"; then wm_subscription_restore_files "$backup"; wm_fail "nginx rejected subscription locations; files restored"; fi
  if ! wm_subscription_validate_public "$metadata"; then wm_nginx_restore_transaction "$transaction" || true; wm_subscription_restore_files "$backup"; wm_fail "Public subscription validation failed; nginx and files restored"; fi
  install -m 0600 "$candidate" "$WM_CONFIG_JSON"; wm_export_config_env_from_json; printf '{"status":"committed"}\n' > "$transaction/result.json"; chmod 600 "$transaction"/*.json
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

wm_subscription_command() { case "${1:-}" in rebuild) shift; wm_subscription_rebuild_command "$@";; validate) shift; wm_subscription_validate_command "$@";; *) wm_fail "Usage: wavemesh subscription rebuild|validate";; esac; }
