#!/usr/bin/env bash

WM_TRANSACTION_TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/transaction_state.py"
WM_TRANSACTION_ROOT="${WM_TRANSACTION_ROOT:-$WM_STATE_DIR/transactions}"
WM_TRANSACTION_KEEP="${WM_TRANSACTION_KEEP:-20}"
WM_ACTIVE_TRANSACTION=""

wm_atomic_install_json() {
  python3 "$WM_TRANSACTION_TOOL" atomic-install --source "$1" --target "$2"
}

wm_lock_mutation() {
  local operation="${1:-mutation}" holder=""
  mkdir -p /run/lock
  exec 9>/run/lock/wavemesh-node.lock
  if ! flock -n 9; then
    [[ -f /run/lock/wavemesh-node.lock.meta ]] && holder="$(tr '\n' ' ' < /run/lock/wavemesh-node.lock.meta 2>/dev/null || true)"
    wm_fail "Another WaveMesh mutation is running${holder:+ (${holder% })}"
  fi
  printf 'pid=%s\noperation=%s\n' "$$" "$operation" > /run/lock/wavemesh-node.lock.meta
  chmod 600 /run/lock/wavemesh-node.lock.meta
}

wm_transaction_snapshot() {
  local transaction="$1" db="" nginx_conf="${WM_NGINX_MANAGED_CONF:-/etc/nginx/wavemesh-managed-locations.conf}"
  cp "$WM_CONFIG_JSON" "$transaction/config.before.json"
  if [[ -f "$WM_RUNTIME_JSON" ]]; then cp "$WM_RUNTIME_JSON" "$transaction/runtime.before.json"; else : > "$transaction/runtime.before.absent"; fi
  if [[ -f "$nginx_conf" ]]; then cp "$nginx_conf" "$transaction/nginx.before.conf"; else : > "$transaction/nginx.before.absent"; fi
  mkdir -p "$transaction/subscriptions.before"
  if [[ -d "$WM_SUB_DIR" ]]; then cp -a "$WM_SUB_DIR/." "$transaction/subscriptions.before/"; else : > "$transaction/subscriptions.before.absent"; fi
  if declare -F wm_xray_get_template >/dev/null && [[ "${NODE_ROLE:-}" == "entry" ]]; then wm_xray_get_template "$transaction/xray.before.json" || return 1; fi
  db="$(python3 - "$WM_CONFIG_JSON" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1],encoding="utf-8"))
print(cfg.get("installation",{}).get("xui",{}).get("database_path", ""))
PY
)"
  if [[ -n "$db" ]]; then
    [[ -f "$db" ]] || { wm_warn "Configured 3X-UI database is missing: ${db}"; return 1; }
    printf '%s\n' "$db" > "$transaction/x-ui.before.db.path"
    python3 - "$db" "$transaction/x-ui.before.db" <<'PY'
import sqlite3,sys
source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2])
try: source.backup(target)
finally: target.close(); source.close()
PY
  fi
  find "$transaction" -type d -exec chmod 700 {} +
  find "$transaction" -type f -exec chmod 600 {} +
}

wm_transaction_begin() {
  local operation="$1" transaction pending=""
  if ! pending="$(python3 "$WM_TRANSACTION_TOOL" check --root "$WM_TRANSACTION_ROOT")"; then
    wm_fail "Incomplete transaction detected: ${pending}. Run: wavemesh transaction recover --id ${pending}"
  fi
  transaction="$(python3 "$WM_TRANSACTION_TOOL" begin --root "$WM_TRANSACTION_ROOT" --operation "$operation" --pid "$$")" || wm_fail "Could not create transaction"
  WM_ACTIVE_TRANSACTION="$transaction"; export WM_ACTIVE_TRANSACTION
  trap 'wm_transaction_exit_handler $?' EXIT
  trap 'exit 130' INT; trap 'exit 143' TERM; trap 'exit 129' HUP
  wm_transaction_snapshot "$transaction" || wm_fail "Could not capture transaction backups"
}

wm_transaction_post_rollback_check() {
  local transaction="$1" readback="$1/xray.rollback.readback.json" check_dir="$1/rollback-subscription-check"
  python3 -m json.tool "$WM_CONFIG_JSON" >/dev/null || return 1
  [[ ! -f "$WM_RUNTIME_JSON" ]] || python3 -m json.tool "$WM_RUNTIME_JSON" >/dev/null || return 1
  nginx -t || return 1
  systemctl is-active --quiet nginx || return 1
  if [[ -f "$transaction/x-ui.before.db" || -f "$transaction/xray.before.json" ]]; then systemctl is-active --quiet x-ui || return 1; fi
  if [[ -f "$transaction/xray.before.json" ]] && declare -F wm_xray_get_template >/dev/null; then
    wm_xray_get_template "$readback" || return 1
    python3 - "$transaction/xray.before.json" "$readback" <<'PY' || return 1
import json,sys
if json.load(open(sys.argv[1],encoding="utf-8")) != json.load(open(sys.argv[2],encoding="utf-8")):
    raise SystemExit("Xray rollback read-back differs from snapshot")
PY
  fi
  if declare -F wm_subscription_prepare >/dev/null && declare -F wm_subscription_validate_public >/dev/null; then
    mkdir -p "$check_dir/rendered"
    wm_subscription_prepare "$WM_CONFIG_JSON" "$check_dir/config.json" "$check_dir/rendered" "$check_dir/metadata.json" || return 1
    wm_subscription_validate_public "$check_dir/metadata.json" || return 1
  fi
}

wm_transaction_wait_xui() {
  local attempt
  for attempt in $(seq 1 20); do
    if systemctl is-active --quiet x-ui; then
      if ! declare -F wm_xui_request_success >/dev/null || wm_xui_request_success GET /panel/api/inbounds/list none >/dev/null; then return 0; fi
    fi
    sleep 1
  done
  return 1
}

wm_transaction_rollback() {
  local transaction="$1" message="${2:-automatic rollback}" failed=0 db="" nginx_conf="${WM_NGINX_MANAGED_CONF:-/etc/nginx/wavemesh-managed-locations.conf}"
  trap - EXIT INT TERM HUP
  python3 "$WM_TRANSACTION_TOOL" mark --transaction "$transaction" --status recovering --message "$message" || failed=1
  [[ ! -f "$transaction/config.before.json" ]] || wm_atomic_install_json "$transaction/config.before.json" "$WM_CONFIG_JSON" || failed=1
  if [[ -f "$transaction/runtime.before.absent" ]]; then rm -f "$WM_RUNTIME_JSON"; elif [[ -f "$transaction/runtime.before.json" ]]; then wm_atomic_install_json "$transaction/runtime.before.json" "$WM_RUNTIME_JSON" || failed=1; fi
  wm_load_config || failed=1
  if [[ -f "$transaction/x-ui.before.db" && -f "$transaction/x-ui.before.db.path" ]]; then
    db="$(cat "$transaction/x-ui.before.db.path")"
    systemctl stop x-ui || failed=1
    install -m 0600 "$transaction/x-ui.before.db" "$db" || failed=1
    systemctl start x-ui || failed=1
    wm_transaction_wait_xui || failed=1
  fi
  if [[ -f "$transaction/xray.before.json" ]] && declare -F wm_xray_apply_template >/dev/null; then wm_xray_apply_template "$transaction/xray.before.json" || failed=1; fi
  if [[ -f "$transaction/nginx.before.absent" ]]; then rm -f "$nginx_conf"; elif [[ -f "$transaction/nginx.before.conf" ]]; then install -m 0644 "$transaction/nginx.before.conf" "$nginx_conf" || failed=1; fi
  rm -rf "$WM_SUB_DIR"
  if [[ ! -f "$transaction/subscriptions.before.absent" ]]; then mkdir -p "$WM_SUB_DIR"; cp -a "$transaction/subscriptions.before/." "$WM_SUB_DIR/" || failed=1; fi
  if [[ -f "$transaction/manifest.output.path" ]]; then rm -f -- "$(cat "$transaction/manifest.output.path")" || failed=1; fi
  nginx -t && systemctl reload nginx || failed=1
  wm_transaction_post_rollback_check "$transaction" || failed=1
  if (( failed == 0 )); then
    python3 "$WM_TRANSACTION_TOOL" mark --transaction "$transaction" --status rolled_back --message "$message"
    wm_warn "Transaction rolled back: $(basename "$transaction")"
  else
    python3 "$WM_TRANSACTION_TOOL" mark --transaction "$transaction" --status rollback_failed --message "$message"
    wm_warn "Rollback needs operator attention: $(basename "$transaction")"
    return 1
  fi
}

wm_transaction_exit_handler() {
  local status="$1"
  trap - EXIT INT TERM HUP
  if (( status != 0 )) && [[ -n "${WM_ACTIVE_TRANSACTION:-}" ]]; then
    if ! wm_transaction_rollback "$WM_ACTIVE_TRANSACTION" "command exited with status ${status}"; then wm_warn "Automatic rollback failed; run wavemesh transaction recover --id $(basename "$WM_ACTIVE_TRANSACTION")"; fi
  fi
  exit "$status"
}

wm_transaction_commit() {
  local transaction="${1:-$WM_ACTIVE_TRANSACTION}"
  find "$transaction" -type d -exec chmod 700 {} +
  find "$transaction" -type f -exec chmod 600 {} +
  python3 "$WM_TRANSACTION_TOOL" mark --transaction "$transaction" --status committed
  WM_ACTIVE_TRANSACTION=""; trap - EXIT INT TERM HUP
  python3 "$WM_TRANSACTION_TOOL" prune --root "$WM_TRANSACTION_ROOT" --keep "$WM_TRANSACTION_KEEP"
  python3 "$WM_TRANSACTION_TOOL" prune-backups --root "$WM_STATE_DIR/backups" --keep "$WM_TRANSACTION_KEEP"
}

wm_transaction_list() {
  local args=(list --root "$WM_TRANSACTION_ROOT")
  if [[ $# -gt 1 || ($# -eq 1 && "$1" != "--json") ]]; then wm_fail "Usage: wavemesh transaction list [--json]"; fi
  [[ "${1:-}" == "--json" ]] && args+=(--json)
  python3 "$WM_TRANSACTION_TOOL" "${args[@]}"
}

wm_transaction_recover() {
  local transaction_id="" transaction
  while [[ $# -gt 0 ]]; do case "$1" in --id) transaction_id="${2:-}"; shift 2;; --latest) transaction_id="$(python3 "$WM_TRANSACTION_TOOL" latest --root "$WM_TRANSACTION_ROOT")" || wm_fail "No incomplete transaction found"; shift;; *) wm_fail "Usage: wavemesh transaction recover --id ID|--latest";; esac; done
  [[ -n "$transaction_id" ]] || wm_fail "Usage: wavemesh transaction recover --id ID|--latest"
  wm_lock_mutation "transaction-recover"
  transaction="$(python3 "$WM_TRANSACTION_TOOL" resolve --root "$WM_TRANSACTION_ROOT" --id "$transaction_id" --active-only)" || wm_fail "Transaction is not recoverable: ${transaction_id}"
  wm_transaction_rollback "$transaction" "operator-requested recovery" || wm_fail "Transaction recovery failed: ${transaction_id}"
  python3 "$WM_TRANSACTION_TOOL" prune --root "$WM_TRANSACTION_ROOT" --keep "$WM_TRANSACTION_KEEP"
  python3 "$WM_TRANSACTION_TOOL" prune-backups --root "$WM_STATE_DIR/backups" --keep "$WM_TRANSACTION_KEEP"
  wm_success "Transaction recovered: ${transaction_id}"
}

wm_transaction_command() {
  case "${1:-}" in list) shift; wm_transaction_list "$@";; recover) shift; wm_transaction_recover "$@";; *) wm_fail "Usage: wavemesh transaction list [--json] | recover --id ID|--latest";; esac
}
