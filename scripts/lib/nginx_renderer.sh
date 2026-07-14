#!/usr/bin/env bash
WM_NGINX_RENDERER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/nginx_renderer.py"
WM_NGINX_MANAGED_CONF="/etc/nginx/wavemesh-managed-locations.conf"
wm_nginx_install_candidate() {
  local candidate="$1" transaction_dir="$2" backup="$transaction_dir/nginx.before.conf"
  if [[ ! -f "$backup" && ! -f "$transaction_dir/nginx.before.absent" ]]; then
    [[ -f "$WM_NGINX_MANAGED_CONF" ]] && cp "$WM_NGINX_MANAGED_CONF" "$backup" || : > "$transaction_dir/nginx.before.absent"
  fi
  install -m 0644 "$candidate" "$WM_NGINX_MANAGED_CONF"
  if ! nginx -t || ! systemctl reload nginx; then
    if [[ -f "$backup" ]]; then install -m 0644 "$backup" "$WM_NGINX_MANAGED_CONF"; else rm -f "$WM_NGINX_MANAGED_CONF"; fi
    if ! nginx -t || ! systemctl reload nginx; then wm_warn "Immediate nginx restore failed; transaction recovery is required"; fi
    return 1
  fi
}
wm_nginx_apply_desired() {
  local config_file="$1" transaction_dir="$2" candidate="$transaction_dir/nginx.candidate.conf"
  python3 "$WM_NGINX_RENDERER" --config "$config_file" --output "$candidate" || return 1
  wm_nginx_install_candidate "$candidate" "$transaction_dir"
}
wm_nginx_apply_native_migration_candidate() {
  local old_config="$1" new_config="$2" transaction_dir="$3" candidate="$transaction_dir/nginx.native-migration.conf" new_path
  new_path="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["network"]["subscription"]["path"])' "$new_config")" || return 1
  python3 "$WM_NGINX_RENDERER" --config "$old_config" --output "$candidate" --additional-native-path "$new_path" || return 1
  wm_nginx_install_candidate "$candidate" "$transaction_dir"
}
wm_nginx_apply_native_rotation_candidate() {
  local config_file="$1" transaction_dir="$2" old_path="$3" new_path="$4" candidate="$transaction_dir/nginx.native-rotation.conf"
  python3 "$WM_NGINX_RENDERER" --config "$config_file" --output "$candidate" --native-alias-from "$old_path" --native-alias-to "$new_path" || return 1
  wm_nginx_install_candidate "$candidate" "$transaction_dir"
}
wm_nginx_restore_transaction() {
  local transaction_dir="$1"; [[ -f "$transaction_dir/nginx.before.conf" ]] || return 0
  install -m 0644 "$transaction_dir/nginx.before.conf" "$WM_NGINX_MANAGED_CONF"; nginx -t && systemctl reload nginx
}
