#!/usr/bin/env bash

WM_STATE_DIR="/etc/wavemesh-node"
WM_REPORT_TXT="/root/wavemesh-node-report.txt"
WM_REPORT_JSON="/root/wavemesh-node-report.json"
WM_SITE_DIR="/var/www/wavemesh-site"
WM_SUB_DIR="/var/www/wavemesh-sub"
WM_CERTBOT_DIR="/var/www/certbot"

DOMAIN=""
EMAIL=""
BRAND="WaveMesh"
CLIENT_COUNT="1"
PUBLIC_IP=""
PUBLIC_PORT="443"
PANEL_PORT=""
PANEL_PATH=""
XHTTP_LOCAL_PORT=""
XHTTP_PATH=""
SUB_PATH=""
SUB_LOCAL_PORT=""
FINGERPRINT="randomized"
NODE_NAME=""
WEB_IDENTITY_NAME=""
PANEL_USERNAME=""
PANEL_PASSWORD=""
PANEL_TOKEN=""
CLIENT_UUIDS=""

wm_banner() {
  cat <<'EOF'
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WaveMesh Node Builder
3X-UI + VLESS XHTTP + nginx + Web Identity + subscriptions
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
}

wm_info() { echo "ℹ  $*"; }
wm_success() { echo "✓  $*"; }
wm_warn() { echo "⚠  $*"; }
wm_fail() { echo "✗  $*" >&2; exit 1; }

wm_random_alnum() {
  local len="${1:-16}"
  openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "$len"
}

wm_random_port() {
  local min="$1"
  local max="$2"
  local p
  for _ in $(seq 1 200); do
    p=$(( min + RANDOM % (max - min + 1) ))
    if ! ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq ":${p}$"; then
      case "$p" in 22|80|443) continue ;; esac
      echo "$p"
      return 0
    fi
  done
  wm_fail "Could not find free port in range ${min}-${max}"
}

wm_parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --domain) DOMAIN="${2:-}"; shift 2 ;;
      --email) EMAIL="${2:-}"; shift 2 ;;
      --brand) BRAND="${2:-WaveMesh}"; shift 2 ;;
      --clients) CLIENT_COUNT="${2:-1}"; shift 2 ;;
      -h|--help)
        cat <<EOF
Usage: sudo bash install.sh --domain example.com --email admin@example.com [--clients 1]
EOF
        exit 0
        ;;
      *) wm_fail "Unknown argument: $1" ;;
    esac
  done
}

wm_collect_inputs() {
  if [[ -z "$DOMAIN" ]]; then
    read -rp "Domain pointed to this VPS: " DOMAIN
  fi
  [[ -n "$DOMAIN" ]] || wm_fail "Domain is required"

  if [[ -z "$EMAIL" ]]; then
    read -rp "Email for Let's Encrypt (optional): " EMAIL || true
  fi

  if ! [[ "$CLIENT_COUNT" =~ ^[0-9]+$ ]] || (( CLIENT_COUNT < 1 || CLIENT_COUNT > 100 )); then
    wm_fail "--clients must be 1..100"
  fi
}

wm_ensure_root() {
  [[ "${EUID}" -eq 0 ]] || wm_fail "Run as root: sudo bash install.sh"
}

wm_prepare_state_dir() {
  mkdir -p "$WM_STATE_DIR" "$WM_SITE_DIR" "$WM_SUB_DIR" "$WM_CERTBOT_DIR"
  chmod 700 "$WM_STATE_DIR"
}

wm_generate_random_values() {
  PANEL_PORT="${PANEL_PORT:-$(wm_random_port 48900 59999)}"
  XHTTP_LOCAL_PORT="${XHTTP_LOCAL_PORT:-$(wm_random_port 10000 30000)}"
  SUB_LOCAL_PORT="${SUB_LOCAL_PORT:-$(wm_random_port 31000 39000)}"
  PANEL_PATH="/${PANEL_PATH:-$(wm_random_alnum 18)}/"
  XHTTP_PATH="/api/$(wm_random_alnum 14)/"
  SUB_PATH="/sub/$(wm_random_alnum 14)/"
  NODE_NAME="${NODE_NAME:-Node-$(wm_random_alnum 6)}"
  WEB_IDENTITY_NAME="${WEB_IDENTITY_NAME:-$(wm_random_company_name)}"
  PANEL_USERNAME="${PANEL_USERNAME:-$(wm_random_alnum 10)}"
  PANEL_PASSWORD="${PANEL_PASSWORD:-$(wm_random_alnum 18)}"
  PANEL_TOKEN="${PANEL_TOKEN:-$(wm_random_alnum 32)}"
}

wm_random_company_name() {
  local a b
  a=(Aster Cloud Nova Vertex Orion Nexora Veloce Atlas Northline BrightLayer Corelink)
  b=(Systems Technologies Digital Networks Labs Solutions Infrastructure Group)
  echo "${a[$RANDOM % ${#a[@]}]} ${b[$RANDOM % ${#b[@]}]}"
}

wm_write_config_env() {
  cat > "$WM_STATE_DIR/config.env" <<EOF
DOMAIN="$DOMAIN"
EMAIL="$EMAIL"
BRAND="$BRAND"
PUBLIC_IP="$PUBLIC_IP"
PUBLIC_PORT="$PUBLIC_PORT"
PANEL_PORT="$PANEL_PORT"
PANEL_PATH="$PANEL_PATH"
XHTTP_LOCAL_PORT="$XHTTP_LOCAL_PORT"
XHTTP_PATH="$XHTTP_PATH"
SUB_PATH="$SUB_PATH"
SUB_LOCAL_PORT="$SUB_LOCAL_PORT"
CLIENT_COUNT="$CLIENT_COUNT"
FINGERPRINT="$FINGERPRINT"
NODE_NAME="$NODE_NAME"
WEB_IDENTITY_NAME="$WEB_IDENTITY_NAME"
PANEL_USERNAME="$PANEL_USERNAME"
PANEL_PASSWORD="$PANEL_PASSWORD"
PANEL_TOKEN="$PANEL_TOKEN"
EOF
  chmod 600 "$WM_STATE_DIR/config.env"
}

wm_load_config() {
  [[ -f "$WM_STATE_DIR/config.env" ]] || wm_fail "Missing $WM_STATE_DIR/config.env"
  # shellcheck disable=SC1091
  source "$WM_STATE_DIR/config.env"
}

wm_install_cli() {
  install -m 0755 "$(pwd)/bin/wavemesh" /usr/local/bin/wavemesh 2>/dev/null || true
}
