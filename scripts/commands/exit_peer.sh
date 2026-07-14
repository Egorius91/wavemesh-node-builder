#!/usr/bin/env bash

wm_require_exit_role() { [[ "$NODE_ROLE" == "exit" ]] || wm_fail "This command requires an exit node"; }

wm_exit_peer_create() {
  local entry_id="" entry_ip="" display_name="" output="" output_dir transaction port path uuid tag clients desired inbound_id candidate manifest
  while [[ $# -gt 0 ]]; do case "$1" in
    --entry-id) entry_id="${2:-}"; shift 2;; --entry-ip) entry_ip="${2:-}"; shift 2;; --display-name) display_name="${2:-}"; shift 2;; --output) output="${2:-}"; shift 2;; *) wm_fail "Unknown exit peer create option: $1";; esac; done
  [[ -n "$entry_id" && -n "$display_name" && -n "$output" ]] || wm_fail "Usage: wavemesh exit peer create --entry-id ID [--entry-ip IP] --display-name TEXT --output FILE"
  output_dir="$(dirname "$output")"; [[ -d "$output_dir" && -w "$output_dir" ]] || wm_fail "Manifest output directory is not writable: $output_dir"
  [[ ! -e "$output" ]] || wm_fail "Refusing to overwrite existing manifest: $output"
  wm_lock_mutation "exit-peer-create"; wm_load_config; wm_require_exit_role; wm_transaction_begin "exit-peer-create"; transaction="$WM_ACTIVE_TRANSACTION"
  port="$(wm_random_port 22000 28999)"; path="/relay/$(wm_random_alnum 18)/"; uuid="$(cat /proc/sys/kernel/random/uuid)"; tag="wm-relay-${entry_id}"
  clients="$transaction/clients.json"; desired="$transaction/inbound.json"; candidate="$transaction/config.candidate.json"; manifest="$transaction/manifest.secret.json"
  UUID="$uuid" TAG="$tag" python3 - <<'PY' > "$clients"
import json,os
print(json.dumps([{"id":os.environ["UUID"],"email":os.environ["TAG"],"enable":True,"flow":"","limitIp":0,"totalGB":0,"expiryTime":0,"tgId":0,"subId":""}]))
PY
  python3 "$WM_INBOUND_TOOL" build --tag "$tag" --remark "--!${tag}" --port "$port" --path "$path" --host "$DOMAIN" --clients "$clients" --output "$desired"
  inbound_id="$(wm_inbound_reconcile "$desired")" || wm_fail "Could not create and verify relay inbound"
  python3 "$WM_EXIT_PEER_TOOL" create --config "$WM_CONFIG_JSON" --candidate "$candidate" --manifest "$manifest" --entry-id "$entry_id" --entry-ip "$entry_ip" --display-name "$display_name" --path "$path" --uuid "$uuid" --port "$port" --inbound-id "$inbound_id" || wm_fail "Could not build relay peer desired state"
  wm_nginx_apply_desired "$candidate" "$transaction" || wm_fail "nginx rejected relay location; transaction will be rolled back"
  wm_atomic_install_json "$candidate" "$WM_CONFIG_JSON"; wm_export_config_env_from_json
  printf '%s\n' "$output" > "$transaction/manifest.output.path"; chmod 600 "$transaction/manifest.output.path"
  install -m 0600 "$manifest" "$output"; wm_transaction_commit "$transaction"
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
  wm_lock_mutation "exit-peer-remove"; wm_load_config; wm_require_exit_role; wm_transaction_begin "exit-peer-remove"; transaction="$WM_ACTIVE_TRANSACTION"; candidate="$transaction/config.candidate.json"
  inbound_id="$(python3 "$WM_EXIT_PEER_TOOL" remove --config "$WM_CONFIG_JSON" --candidate "$candidate" --entry-id "$entry_id")" || wm_fail "Could not build peer removal"
  wm_nginx_apply_desired "$candidate" "$transaction" || wm_fail "nginx rejected peer removal"
  wm_inbound_delete "$inbound_id" || wm_fail "Could not delete relay inbound; transaction will be rolled back"
  wm_atomic_install_json "$candidate" "$WM_CONFIG_JSON"; wm_export_config_env_from_json; wm_transaction_commit "$transaction"
  wm_success "Relay peer removed: ${entry_id}"
}

wm_exit_peer_command() { case "${1:-}" in create) shift; wm_exit_peer_create "$@";; list) shift; wm_exit_peer_list "$@";; remove) shift; wm_exit_peer_remove "$@";; *) wm_fail "Usage: wavemesh exit peer create|list|remove";; esac; }
