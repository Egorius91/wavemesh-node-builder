#!/usr/bin/env bash
WM_NGINX_RENDERER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/nginx_renderer.py"
WM_NGINX_SITE_TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/nginx_site.py"
WM_NGINX_MANAGED_CONF="/etc/nginx/wavemesh-managed-locations.conf"
WM_NGINX_SITE_CONF="/etc/nginx/sites-available/wavemesh-node.conf"

wm_nginx_sanitize_subscription_site() {
  local config_file="$1" transaction_dir="$2" candidate
  [[ -f "$WM_NGINX_SITE_CONF" ]] || return 0
  candidate="$transaction_dir/nginx.site.candidate.conf"
  python3 "$WM_NGINX_SITE_TOOL" --site "$WM_NGINX_SITE_CONF" --config "$config_file" --output "$candidate" || return 1
  install -m 0644 "$candidate" "$WM_NGINX_SITE_CONF"
}
wm_nginx_apply_desired() {
  local config_file="$1" transaction_dir="$2" candidate backup
  candidate="$transaction_dir/nginx.candidate.conf"; backup="$transaction_dir/nginx.before.conf"
  python3 "$WM_NGINX_RENDERER" --config "$config_file" --output "$candidate" || return 1
  [[ -f "$WM_NGINX_MANAGED_CONF" ]] && cp "$WM_NGINX_MANAGED_CONF" "$backup" || : > "$backup"
  install -m 0644 "$candidate" "$WM_NGINX_MANAGED_CONF"
  if ! nginx -t || ! systemctl reload nginx; then
    install -m 0644 "$backup" "$WM_NGINX_MANAGED_CONF"
    if ! nginx -t || ! systemctl reload nginx; then wm_warn "Immediate nginx restore failed; transaction recovery is required"; fi
    return 1
  fi
}
wm_nginx_restore_transaction() {
  local transaction_dir="$1"; [[ -f "$transaction_dir/nginx.before.conf" ]] || return 0
  install -m 0644 "$transaction_dir/nginx.before.conf" "$WM_NGINX_MANAGED_CONF"; nginx -t && systemctl reload nginx
}
