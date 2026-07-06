#!/usr/bin/env bash

XUI_INSTALL_URL="${XUI_INSTALL_URL:-https://raw.githubusercontent.com/MHSanaei/3x-ui/master/install.sh}"
XUI_BIN="${XUI_BIN:-/usr/local/x-ui/x-ui}"
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

wm_install_3xui() {
  wm_info "Installing 3X-UI"
  if command -v x-ui >/dev/null 2>&1 || [[ -x "$XUI_BIN" ]]; then
    wm_success "3X-UI already appears to be installed"
    return 0
  fi

  local tmp_script="/tmp/wavemesh-3xui-install.sh"
  curl -fsSL "$XUI_INSTALL_URL" -o "$tmp_script" || wm_fail "Could not download 3X-UI installer from $XUI_INSTALL_URL"
  chmod +x "$tmp_script"

  # The installer behavior changes across 3X-UI releases. We try a non-interactive environment first,
  # then fall back to piping safe defaults if the upstream script asks questions.
  if XUI_NONINTERACTIVE=1 \
     XUI_USERNAME="$PANEL_USERNAME" \
     XUI_PASSWORD="$PANEL_PASSWORD" \
     XUI_PORT="$PANEL_PORT" \
     XUI_WEBBASEPATH="$PANEL_PATH" \
     bash "$tmp_script"; then
    wm_success "3X-UI installer completed"
  else
    wm_warn "Non-interactive 3X-UI install failed; trying conservative piped defaults"
    printf 'n\n' | bash "$tmp_script" || wm_fail "3X-UI installation failed"
  fi

  systemctl enable --now "$XUI_SERVICE" >/dev/null 2>&1 || true
  sleep 2
  if systemctl is-active --quiet "$XUI_SERVICE"; then
    wm_success "3X-UI service is active"
  else
    wm_warn "3X-UI service is not active yet; diagnostics will report details"
  fi
}

wm_configure_3xui_panel() {
  wm_info "Configuring 3X-UI panel access"

  # Best-effort CLI configuration. Different 3X-UI builds expose different CLI names/flags,
  # so every command is non-fatal and followed by diagnostics/reporting.
  if command -v x-ui >/dev/null 2>&1; then
    x-ui settings -username "$PANEL_USERNAME" -password "$PANEL_PASSWORD" >/dev/null 2>&1 || true
    x-ui settings -port "$PANEL_PORT" >/dev/null 2>&1 || true
    x-ui settings -webBasePath "$PANEL_PATH" >/dev/null 2>&1 || true
    x-ui restart >/dev/null 2>&1 || systemctl restart "$XUI_SERVICE" >/dev/null 2>&1 || true
  elif [[ -x "$XUI_BIN" ]]; then
    "$XUI_BIN" settings -username "$PANEL_USERNAME" -password "$PANEL_PASSWORD" >/dev/null 2>&1 || true
    "$XUI_BIN" settings -port "$PANEL_PORT" >/dev/null 2>&1 || true
    "$XUI_BIN" settings -webBasePath "$PANEL_PATH" >/dev/null 2>&1 || true
    systemctl restart "$XUI_SERVICE" >/dev/null 2>&1 || true
  fi

  local db
  db="$(wm_find_xui_db || true)"
  if [[ -n "$db" ]]; then
    cp "$db" "${db}.wavemesh.$(date +%Y%m%d%H%M%S).bak" || true
    echo "XUI_DB=\"${db}\"" >> "$WM_STATE_DIR/config.env"
    wm_success "Detected 3X-UI database: $db"
  else
    wm_warn "Could not detect 3X-UI database automatically"
  fi

  wm_success "3X-UI panel configuration step completed"
}
