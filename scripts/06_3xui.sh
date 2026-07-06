#!/usr/bin/env bash

XUI_INSTALL_URL="${XUI_INSTALL_URL:-https://raw.githubusercontent.com/MHSanaei/3x-ui/master/install.sh}"
XUI_MANAGER_BIN="${XUI_MANAGER_BIN:-/usr/bin/x-ui}"
XUI_RUNTIME_BIN="${XUI_RUNTIME_BIN:-/usr/local/x-ui/x-ui}"
XUI_SERVICE="${XUI_SERVICE:-x-ui}"
XUI_DB_CANDIDATES=(
  "/etc/x-ui/x-ui.db"
  "/usr/local/x-ui/bin/x-ui.db"
  "/usr/local/x-ui/x-ui.db"
)

wm_find_xui_db() {
  local p
  for p in "${XUI_DB_CANDIDATES[@]}"; do
    [[ -f "$p" ]] && { echo "$p"; return 0; }
  done
  find /etc /usr/local -maxdepth 4 -type f -name '*x-ui*.db' 2>/dev/null | head -n1
}

wm_xui_panel_installed() {
  systemctl list-unit-files 2>/dev/null | grep -q '^x-ui.service' || [[ -f /etc/systemd/system/x-ui.service ]]
}

wm_xui_manager_ready() {
  command -v x-ui >/dev/null 2>&1 || [[ -x "$XUI_MANAGER_BIN" ]]
}

wm_xui_manager_cmd() {
  if command -v x-ui >/dev/null 2>&1; then
    echo "x-ui"
  else
    echo "$XUI_MANAGER_BIN"
  fi
}

wm_install_xui_manager() {
  if wm_xui_manager_ready; then
    wm_success "3X-UI manager already exists: $(wm_xui_manager_cmd)"
    return 0
  fi

  wm_info "Downloading 3X-UI manager/install script"
  curl -fsSL "$XUI_INSTALL_URL" -o "$XUI_MANAGER_BIN" || wm_fail "Could not download 3X-UI installer from $XUI_INSTALL_URL"
  chmod +x "$XUI_MANAGER_BIN"
  wm_success "3X-UI manager installed at $XUI_MANAGER_BIN"
}

wm_run_xui_interactive_install_automated() {
  local cmd
  cmd="$(wm_xui_manager_cmd)"

  wm_info "Running automated 3X-UI v3.4.x install flow"
  wm_info "Target choices: SQLite, port ${PANEL_PORT}, generated credentials, path ${PANEL_PATH}, skip internal SSL, bind 127.0.0.1"

  # Observed v3.4.x prompts:
  # 1) main menu -> Install
  # 2) database -> SQLite
  # 3) customize panel port -> yes
  # 4) port
  # 5) username
  # 6) password
  # 7) web base path
  # 8) SSL method -> Skip SSL
  # 9) bind panel to 127.0.0.1 only -> yes
  # Some releases may skip/insert minor prompts; this script intentionally fails later if service/API is not ready.
  local answers
  answers=$(cat <<EOF
1
1
y
${PANEL_PORT}
${PANEL_USERNAME}
${PANEL_PASSWORD}
${PANEL_PATH}
4
y
EOF
)
  printf '%s' "$answers" | "$cmd" install || return 1
}

wm_wait_for_xui_service() {
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl enable --now "$XUI_SERVICE" >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    if systemctl is-active --quiet "$XUI_SERVICE"; then
      wm_success "3X-UI service is active"
      return 0
    fi
    sleep 1
  done
  return 1
}

wm_assert_xui_bound_to_loopback() {
  local listen
  listen="$(ss -ltnp 2>/dev/null | grep ":${PANEL_PORT} " || true)"
  if [[ -z "$listen" ]]; then
    wm_warn "Could not confirm panel listen socket on port ${PANEL_PORT}"
    return 0
  fi
  if grep -q "127.0.0.1:${PANEL_PORT}" <<< "$listen"; then
    wm_success "3X-UI panel is bound to 127.0.0.1:${PANEL_PORT}"
  else
    wm_warn "3X-UI panel may not be loopback-only. Listen output: $listen"
  fi
}

wm_update_config_json_xui_installation() {
  local db="$1"
  local status="$2"
  python3 - <<PY
import json, subprocess
path = "$WM_CONFIG_JSON"
cfg = json.load(open(path, encoding="utf-8"))
try:
    raw = subprocess.check_output(["x-ui", "status"], stderr=subprocess.STDOUT, text=True, timeout=5)
except Exception:
    raw = ""
cfg.setdefault("installation", {})["xui"] = {
  "version_hint": "3.4.x",
  "database": "sqlite",
  "database_path": "$db",
  "panel_bind": "127.0.0.1",
  "ssl_mode": "external-nginx",
  "service": "$XUI_SERVICE",
  "status": "$status"
}
cfg["panel"]["internal_url"] = "http://127.0.0.1:$PANEL_PORT$PANEL_PATH"
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
  chmod 600 "$WM_CONFIG_JSON"
  wm_export_config_env_from_json
}

wm_install_3xui() {
  wm_info "Installing 3X-UI"
  wm_install_xui_manager

  if wm_xui_panel_installed && systemctl is-active --quiet "$XUI_SERVICE"; then
    wm_success "3X-UI panel already installed and running"
  else
    wm_run_xui_interactive_install_automated || wm_fail "Automated 3X-UI installation failed"
    wm_wait_for_xui_service || wm_fail "3X-UI service did not become active after installation"
  fi

  wm_assert_xui_bound_to_loopback
}

wm_configure_3xui_panel() {
  wm_info "Inspecting 3X-UI panel installation"

  local db=""
  db="$(wm_find_xui_db || true)"
  if [[ -n "$db" ]]; then
    mkdir -p "$WM_STATE_DIR/backups"
    cp "$db" "$WM_STATE_DIR/backups/x-ui.$(date +%Y%m%d%H%M%S).db.bak" || true
    echo "XUI_DB=\"${db}\"" >> "$WM_STATE_DIR/config.env"
    wm_success "Detected 3X-UI database: $db"
  else
    wm_warn "Could not detect 3X-UI database automatically"
  fi

  if systemctl is-active --quiet "$XUI_SERVICE"; then
    wm_update_config_json_xui_installation "$db" "running"
    wm_success "3X-UI panel configuration recorded in config.json"
  else
    wm_update_config_json_xui_installation "$db" "not-running"
    wm_fail "3X-UI service is not running"
  fi
}
