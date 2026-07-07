#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$PROJECT_DIR/scripts"

source "$SCRIPTS_DIR/00_common.sh"
source "$SCRIPTS_DIR/01_preflight.sh"
source "$SCRIPTS_DIR/02_firewall.sh"
source "$SCRIPTS_DIR/03_bbr.sh"
source "$SCRIPTS_DIR/04_web_identity.sh"
source "$SCRIPTS_DIR/05_nginx_ssl.sh"
source "$SCRIPTS_DIR/06_3xui.sh"
source "$SCRIPTS_DIR/07_xhttp_inbound.sh"
source "$SCRIPTS_DIR/08_subscriptions.sh"
source "$SCRIPTS_DIR/09_report.sh"
source "$SCRIPTS_DIR/10_diagnostics.sh"

main() {
  wm_banner
  wm_parse_args "$@"
  wm_ensure_root
  wm_collect_inputs
  wm_prepare_state_dir
  wm_preflight_os
  wm_detect_public_ip
  wm_validate_dns
  wm_generate_random_values
  wm_write_config_env

  wm_install_packages
  wm_setup_firewall
  wm_enable_bbr
  wm_generate_web_identity
  wm_configure_nginx_http
  wm_obtain_ssl
  wm_configure_nginx_https

  wm_create_clients
  wm_install_3xui
  wm_configure_3xui_panel
  wm_create_xhttp_inbound
  wm_generate_fallback_subscription
  wm_validate_subscription_output
  wm_run_diagnostics
  wm_write_reports
  wm_print_telegram_bot_connection_info
  wm_install_cli

  wm_success "WaveMesh node builder completed. Report: /root/wavemesh-node-report.txt"
}

main "$@"
