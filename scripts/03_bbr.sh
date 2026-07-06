#!/usr/bin/env bash

wm_enable_bbr() {
  wm_info "Enabling BBR"
  cat >/etc/sysctl.d/99-wavemesh-bbr.conf <<'EOF'
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
  sysctl --system >/dev/null || true
  if sysctl net.ipv4.tcp_congestion_control | grep -q bbr; then
    wm_success "BBR enabled"
  else
    wm_warn "BBR was not confirmed; continuing"
  fi
}
