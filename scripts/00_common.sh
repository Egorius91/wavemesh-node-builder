#!/usr/bin/env bash

WM_STATE_DIR="/etc/wavemesh-node"
WM_CONFIG_JSON="$WM_STATE_DIR/config.json"
WM_RUNTIME_JSON="$WM_STATE_DIR/runtime.json"
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
PEXELS_API_KEY="${PEXELS_API_KEY:-}"
SITE_THEME="${SITE_THEME:-auto}"

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

wm_json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().rstrip("\n")))'
}

wm_parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --domain) DOMAIN="${2:-}"; shift 2 ;;
      --email) EMAIL="${2:-}"; shift 2 ;;
      --brand) BRAND="${2:-WaveMesh}"; shift 2 ;;
      --clients) CLIENT_COUNT="${2:-1}"; shift 2 ;;
      --pexels-key) PEXELS_API_KEY="${2:-}"; shift 2 ;;
      --site-theme) SITE_THEME="${2:-auto}"; shift 2 ;;
      -h|--help)
        cat <<EOF
Usage: sudo bash install.sh --domain example.com --email admin@example.com [--clients 1] [--pexels-key KEY] [--site-theme auto|logistics|architecture|coffee|energy|legalops|studio|wellness|education|finance|gardening]
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

  if [[ -z "${PEXELS_API_KEY:-}" ]]; then
    read -rsp "Pexels API key for real site photos (optional, press Enter to skip): " PEXELS_API_KEY || true
    echo
  fi

  if [[ -z "${SITE_THEME:-}" || "$SITE_THEME" == "auto" ]]; then
    cat <<'EOF'
Choose cover-site theme:
  0) Auto / surprise me
  1) Logistics operations
  2) Architecture studio
  3) Coffee roastery
  4) Energy advisory
  5) Legal operations
  6) Design studio
  7) Wellness clinic
  8) Education lab
  9) Finance office
 10) Gardening studio
EOF
    local theme_choice
    read -rp "Theme [0-10, default 0]: " theme_choice || true
    case "${theme_choice:-0}" in
      0|"") SITE_THEME="auto" ;;
      1) SITE_THEME="logistics" ;;
      2) SITE_THEME="architecture" ;;
      3) SITE_THEME="coffee" ;;
      4) SITE_THEME="energy" ;;
      5) SITE_THEME="legalops" ;;
      6) SITE_THEME="studio" ;;
      7) SITE_THEME="wellness" ;;
      8) SITE_THEME="education" ;;
      9) SITE_THEME="finance" ;;
      10) SITE_THEME="gardening" ;;
      *) wm_fail "Invalid site theme choice: ${theme_choice}" ;;
    esac
  fi

  case "$SITE_THEME" in
    auto|logistics|architecture|coffee|energy|legalops|studio|wellness|education|finance|gardening) ;;
    *) wm_fail "Invalid --site-theme: $SITE_THEME" ;;
  esac

  if ! [[ "$CLIENT_COUNT" =~ ^[0-9]+$ ]] || (( CLIENT_COUNT < 1 || CLIENT_COUNT > 100 )); then
    wm_fail "--clients must be 1..100"
  fi
}

wm_ensure_root() {
  [[ "${EUID}" -eq 0 ]] || wm_fail "Run as root: sudo bash install.sh"
}

wm_prepare_state_dir() {
  mkdir -p "$WM_STATE_DIR" "$WM_SITE_DIR" "$WM_SUB_DIR" "$WM_CERTBOT_DIR" "$WM_STATE_DIR/backups"
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

wm_write_config_json() {
  local installed_at hostname timezone
  installed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  hostname="$(hostname)"
  timezone="$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)"
  python3 - <<PY
import json
cfg = {
  "version": 1,
  "builder": {"name": "WaveMesh Node Builder", "version": "0.1.0", "installed_at": "$installed_at"},
  "server": {"hostname": "$hostname", "public_ip": "$PUBLIC_IP", "domain": "$DOMAIN", "timezone": "$timezone"},
  "network": {
    "http_port": 80,
    "public_port": 443,
    "xhttp": {"listen": "127.0.0.1", "port": int("$XHTTP_LOCAL_PORT"), "path": "$XHTTP_PATH"},
    "subscription": {"path": "$SUB_PATH", "mode": "generated", "local_port": int("$SUB_LOCAL_PORT")}
  },
  "panel": {"type": "3x-ui", "listen_port": int("$PANEL_PORT"), "path": "$PANEL_PATH", "username": "$PANEL_USERNAME", "password": "$PANEL_PASSWORD", "token": "$PANEL_TOKEN"},
  "tls": {"provider": "letsencrypt", "email": "$EMAIL", "certificate_path": "/etc/letsencrypt/live/$DOMAIN/fullchain.pem", "key_path": "/etc/letsencrypt/live/$DOMAIN/privkey.pem"},
  "web_identity": {"company_name": "$WEB_IDENTITY_NAME", "theme": "$SITE_THEME", "site_path": "$WM_SITE_DIR"},
  "clients": [],
  "diagnostics": {"last_check": None, "status": "pending"}
}
with open("$WM_CONFIG_JSON", "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
  chmod 600 "$WM_CONFIG_JSON"
}

wm_export_config_env_from_json() {
  [[ -f "$WM_CONFIG_JSON" ]] || wm_fail "Missing $WM_CONFIG_JSON"
  python3 - <<PY > "$WM_STATE_DIR/config.env"
import json
cfg=json.load(open("$WM_CONFIG_JSON", encoding="utf-8"))
clients=cfg.get("clients", [])
vals={
"DOMAIN": cfg["server"]["domain"],
"EMAIL": cfg["tls"].get("email", ""),
"BRAND": "$BRAND",
"PUBLIC_IP": cfg["server"].get("public_ip", ""),
"PUBLIC_PORT": str(cfg["network"].get("public_port", 443)),
"PANEL_PORT": str(cfg["panel"]["listen_port"]),
"PANEL_PATH": cfg["panel"]["path"],
"XHTTP_LOCAL_PORT": str(cfg["network"]["xhttp"]["port"]),
"XHTTP_PATH": cfg["network"]["xhttp"]["path"],
"SUB_PATH": cfg["network"]["subscription"]["path"],
"SUB_LOCAL_PORT": str(cfg["network"]["subscription"].get("local_port", "")),
"CLIENT_COUNT": str(max(len(clients), int("$CLIENT_COUNT"))),
"FINGERPRINT": "$FINGERPRINT",
"NODE_NAME": "$NODE_NAME",
"WEB_IDENTITY_NAME": cfg["web_identity"]["company_name"],
"SITE_THEME": cfg.get("web_identity", {}).get("theme", "auto"),
"PANEL_USERNAME": cfg["panel"]["username"],
"PANEL_PASSWORD": cfg["panel"]["password"],
"PANEL_TOKEN": cfg["panel"].get("token", ""),
"CLIENT_UUIDS": ",".join(c.get("uuid", "") for c in clients if c.get("uuid")),
}
for k,v in vals.items():
    print(f'{k}="{str(v).replace(chr(34), chr(92)+chr(34))}"')
PY
  chmod 600 "$WM_STATE_DIR/config.env"
}

wm_write_config_env() {
  wm_write_config_json
  wm_export_config_env_from_json
}

wm_config_json_set_clients_from_csv() {
  local csv="$1"
  python3 - <<PY
import json
path="$WM_CONFIG_JSON"
cfg=json.load(open(path, encoding="utf-8"))
uuids=[u for u in "$csv".split(',') if u]
cfg["clients"]=[{"name": f"Client-{i+1}", "uuid": u, "enabled": True} for i,u in enumerate(uuids)]
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
  chmod 600 "$WM_CONFIG_JSON"
  wm_export_config_env_from_json
}

wm_load_config() {
  if [[ -f "$WM_CONFIG_JSON" ]]; then
    wm_export_config_env_from_json
  fi
  [[ -f "$WM_STATE_DIR/config.env" ]] || wm_fail "Missing config: $WM_CONFIG_JSON or $WM_STATE_DIR/config.env"
  # shellcheck disable=SC1091
  source "$WM_STATE_DIR/config.env"
}

wm_install_cli() {
  install -m 0755 "$(pwd)/bin/wavemesh" /usr/local/bin/wavemesh 2>/dev/null || true
}
