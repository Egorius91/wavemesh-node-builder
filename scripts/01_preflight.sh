#!/usr/bin/env bash

wm_preflight_os() {
  wm_info "Checking OS"
  [[ -f /etc/os-release ]] || wm_fail "Unsupported OS"
  # shellcheck disable=SC1091
  source /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) wm_success "Supported OS: $PRETTY_NAME" ;;
    *) wm_fail "Only Ubuntu/Debian are supported in MVP. Detected: ${PRETTY_NAME:-unknown}" ;;
  esac
}

wm_install_packages() {
  wm_info "Installing packages"
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y curl wget jq openssl ca-certificates gnupg lsb-release ufw nginx certbot python3 python3-venv sqlite3 qrencode dnsutils netcat-openbsd
  wm_success "Packages installed"
}

wm_detect_public_ip() {
  PUBLIC_IP="$(curl -4fsS https://api.ipify.org || true)"
  [[ -n "$PUBLIC_IP" ]] || PUBLIC_IP="$(hostname -I | awk '{print $1}')"
  [[ -n "$PUBLIC_IP" ]] || wm_fail "Could not detect public IP"
  wm_success "Public IP: $PUBLIC_IP"
}

wm_validate_dns() {
  wm_info "Checking DNS A-record for $DOMAIN"
  local dns_ip
  dns_ip="$(dig +short A "$DOMAIN" | tail -n1 || true)"
  [[ -n "$dns_ip" ]] || wm_fail "Domain $DOMAIN has no A-record"
  if [[ "$dns_ip" != "$PUBLIC_IP" ]]; then
    wm_warn "DNS A-record points to $dns_ip, current VPS IP is $PUBLIC_IP"
    wm_warn "SSL may fail unless DNS has propagated. Continue only if this is expected."
  else
    wm_success "DNS points to this VPS"
  fi
}
