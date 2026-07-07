#!/usr/bin/env bash

wm_detect_ssh_port() {
  local port
  port="$(ss -ltnp 2>/dev/null | awk '/sshd/ {print $4}' | sed 's/.*://' | head -n1)"
  echo "${port:-22}"
}

wm_setup_firewall() {
  wm_info "Configuring firewall"
  local ssh_port
  ssh_port="$(wm_detect_ssh_port)"
  ufw allow "${ssh_port}/tcp" >/dev/null || true
  ufw allow 80/tcp >/dev/null || true
  ufw allow 443/tcp >/dev/null || true
  [[ -n "${PANEL_PORT:-}" ]] && ufw deny "${PANEL_PORT}/tcp" >/dev/null || true
  ufw deny 2096/tcp >/dev/null || true
  ufw --force enable >/dev/null || true
  wm_success "Firewall configured: SSH ${ssh_port}, 80/tcp, 443/tcp allowed; panel and 3X-UI built-in subscription ports denied externally"
}

wm_check_provider_ports_hint() {
  wm_info "If HTTPS/SSL fails, check provider firewall: open TCP 80 and TCP 443 in VPS control panel."
}
