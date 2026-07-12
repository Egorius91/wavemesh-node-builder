#!/usr/bin/env bash

wm_require_exit_role() { [[ "$NODE_ROLE" == "exit" ]] || wm_fail "This command requires an exit node"; }
wm_lock_mutation() { mkdir -p /run/lock; exec 9>/run/lock/wavemesh-node.lock; flock -n 9 || wm_fail "Another WaveMesh mutation is running"; }
wm_transaction_dir() { local dir="$WM_STATE_DIR/transactions/$(date -u +%Y%m%dT%H%M%SZ)-$(wm_random_alnum 6)"; mkdir -p "$dir"; chmod 700 "$WM_STATE_DIR/transactions" "$dir"; printf '%s' "$dir"; }

wm_exit_peer_create() {
  local entry_id="" entry_ip="" display_name="" output="" output_dir transaction port path uuid tag clients desired inbound_id candidate manifest
  while [[ $# -gt 0 ]]; do case "$1" in
    --entry-id) entry_id="${2:-}"; shift 2;; --entry-ip) entry_ip="${2:-}"; shift 2;; --display-name) display_name="${2:-}"; shift 2;; --output) output="${2:-}"; shift 2;; *) wm_fail "Unknown exit peer create option: $1";; esac; done
  [[ -n "$entry_id" && -n "$display_name" && -n "$output" ]] || wm_fail "Usage: wavemesh exit peer create --entry-id ID [--entry-ip IP] --display-name TEXT --output FILE"
  output_dir="$(dirname "$output")"; [[ -d "$output_dir" && -w "$output_dir" ]] || wm_fail "Manifest output directory is not writable: $output_dir"
  [[ ! -e "$output" ]] || wm_fail "Refusing to overwrite existing manifest: $output"
  wm_lock_mutation; wm_load_config; wm_require_exit_role; transaction="$(wm_transaction_dir)"; cp "$WM_CONFIG_JSON" "$transaction/config.before.json"; chmod 600 "$transaction/config.before.json"
  port="$(wm_random_port 22000 28999)"; path="/relay/$(wm_random_alnum 18)/"; uuid="$(cat /proc/sys/kernel/random/uuid)"; tag="wm-relay-${entry_id}"
  clients="$transaction/clients.json"; desired="$transaction/inbound.json"; candidate="$transaction/config.candidate.json"; manifest="$transaction/manifest.secret.json"
  UUID="$uuid" TAG="$tag" python3 - <<'PY' > "$clients"
import json,os
print(json.dumps([{"id":os.environ["UUID"],"email":os.environ["TAG"],"enable":True,"flow":"","limitIp":0,"totalGB":0,"expiryTime":0,"tgId":"","subId":""}]))
PY
  python3 "$WM_INBOUND_TOOL" build --tag "$tag" --port "$port" --path "$path" --host "$DOMAIN" --clients "$clients" --output "$desired"
  inbound_id="$(wm_inbound_reconcile "$desired")" || wm_fail "Could not create and verify relay inbound"
  if ! python3 "$WM_EXIT_PEER_TOOL" create --config "$WM_CONFIG_JSON" --candidate "$candidate" --manifest "$manifest" --entry-id "$entry_id" --entry-ip "$entry_ip" --display-name "$display_name" --path "$path" --uuid "$uuid" --port "$port" --inbound-id "$inbound_id"; then wm_inbound_delete "$inbound_id" || true; wm_fail "Could not build relay peer desired state"; fi
  if ! wm_nginx_apply_desired "$candidate" "$transaction"; then wm_inbound_delete "$inbound_id" || true; wm_fail "nginx rejected relay location; relay inbound removed"; fi
  install -m 0600 "$candidate" "$WM_CONFIG_JSON"; wm_export_config_env_from_json
  install -m 0600 "$manifest" "$output"; printf '{"status":"committed"}\n' > "$transaction/result.json"; chmod 600 "$transaction"/*.json
  wm_success "Relay peer created: ${entry_id}"; wm_success "Join manifest written: ${output}"; wm_info "Manifest contains secrets and was not printed"
}

wm_exit_peer_list() {
  wm_load_config; wm_require_exit_role
  python3 - "$WM_CONFIG_JSON" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1],encoding="utf-8")); peers=cfg.get("relay_peers",[])
if not peers: print("No relay peers")
for p in peers: print(f"{p['id']}\t{'enabled' if p.get('enabled',True) else 'disabled'}\t{p.get('display_name','')}\t127.0.0.1:{p['inbound']['local_port']}")
PY
}

wm_exit_peer_remove() {
  local entry_id="" transaction candidate inbound_id
  while [[ $# -gt 0 ]]; do case "$1" in --entry-id) entry_id="${2:-}"; shift 2;; *) wm_fail "Unknown exit peer remove option: $1";; esac; done
  [[ -n "$entry_id" ]] || wm_fail "Usage: wavemesh exit peer remove --entry-id ID"
  wm_lock_mutation; wm_load_config; wm_require_exit_role; transaction="$(wm_transaction_dir)"; candidate="$transaction/config.candidate.json"; cp "$WM_CONFIG_JSON" "$transaction/config.before.json"
  inbound_id="$(python3 "$WM_EXIT_PEER_TOOL" remove --config "$WM_CONFIG_JSON" --candidate "$candidate" --entry-id "$entry_id")" || wm_fail "Could not build peer removal"
  wm_nginx_apply_desired "$candidate" "$transaction" || wm_fail "nginx rejected peer removal"
  if ! wm_inbound_delete "$inbound_id"; then wm_nginx_restore_transaction "$transaction" || true; wm_fail "Could not delete relay inbound; nginx was restored"; fi
  install -m 0600 "$candidate" "$WM_CONFIG_JSON"; wm_export_config_env_from_json; printf '{"status":"committed"}\n' > "$transaction/result.json"; chmod 600 "$transaction"/*.json
  wm_success "Relay peer removed: ${entry_id}"
}

wm_exit_peer_command() { case "${1:-}" in create) shift; wm_exit_peer_create "$@";; list) shift; wm_exit_peer_list "$@";; remove) shift; wm_exit_peer_remove "$@";; *) wm_fail "Usage: wavemesh exit peer create|list|remove";; esac; }
