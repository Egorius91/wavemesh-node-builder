#!/usr/bin/env bash

XUI_REPO="${XUI_REPO:-MHSanaei/3x-ui}"
XUI_RELEASE_TAG="${XUI_RELEASE_TAG:-latest}"
XUI_MANAGER_BIN="${XUI_MANAGER_BIN:-/usr/bin/x-ui}"
XUI_HOME="${XUI_HOME:-/usr/local/x-ui}"
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

wm_detect_xui_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo "amd64" ;;
    aarch64|arm64) echo "arm64" ;;
    armv7l|armv7*) echo "armv7" ;;
    armv6l|armv6*) echo "armv6" ;;
    *) wm_fail "Unsupported architecture for 3X-UI release asset: $(uname -m)" ;;
  esac
}

wm_xui_panel_installed() {
  [[ -x "$XUI_RUNTIME_BIN" ]] && [[ -f /etc/systemd/system/x-ui.service || -f /usr/lib/systemd/system/x-ui.service ]]
}

wm_get_latest_xui_release_json() {
  if [[ "$XUI_RELEASE_TAG" == "latest" ]]; then
    curl -fsSL "https://api.github.com/repos/${XUI_REPO}/releases/latest"
  else
    curl -fsSL "https://api.github.com/repos/${XUI_REPO}/releases/tags/${XUI_RELEASE_TAG}"
  fi
}

wm_select_xui_asset_url() {
  local arch="$1"
  python3 - "$arch" <<'PY'
import json, sys
arch = sys.argv[1]
data = json.load(sys.stdin)
assets = data.get("assets", [])
patterns = [
    f"linux-{arch}",
    f"linux_{arch}",
    f"linux.{arch}",
    f"x-ui-{arch}",
    arch,
]
for asset in assets:
    name = asset.get("name", "").lower()
    if not any(name.endswith(ext) or ext in name for ext in (".tar.gz", ".tgz", ".zip")):
        continue
    if "linux" in name and any(p in name for p in patterns):
        print(asset.get("browser_download_url", ""))
        sys.exit(0)
for asset in assets:
    name = asset.get("name", "").lower()
    if "linux" in name and arch in name:
        print(asset.get("browser_download_url", ""))
        sys.exit(0)
sys.exit(1)
PY
}

wm_get_xui_release_tag_from_json() {
  python3 - <<'PY'
import json, sys
data=json.load(sys.stdin)
print(data.get("tag_name", "unknown"))
PY
}

wm_download_latest_xui_release() {
  local arch release_json asset_url tag workdir archive
  arch="$(wm_detect_xui_arch)"
  wm_info "Resolving latest stable 3X-UI release for linux-${arch}"
  release_json="$(wm_get_latest_xui_release_json)" || wm_fail "Could not query GitHub releases for ${XUI_REPO}"
  tag="$(printf '%s' "$release_json" | wm_get_xui_release_tag_from_json)"
  asset_url="$(printf '%s' "$release_json" | wm_select_xui_asset_url "$arch")" || wm_fail "Could not find linux-${arch} asset in ${XUI_REPO} ${tag}"

  wm_info "Selected 3X-UI release ${tag}: ${asset_url}"
  workdir="/tmp/wavemesh-xui-${tag}"
  archive="${workdir}/x-ui-release"
  rm -rf "$workdir"
  mkdir -p "$workdir"
  curl -fL "$asset_url" -o "$archive" || wm_fail "Could not download 3X-UI release asset"

  case "$asset_url" in
    *.zip) unzip -q "$archive" -d "$workdir/extract" ;;
    *) mkdir -p "$workdir/extract" && tar -xzf "$archive" -C "$workdir/extract" ;;
  esac

  echo "$workdir/extract|$tag"
}

wm_install_xui_files_from_release() {
  local extract_dir="$1"
  local tag="$2"
  local src_dir src_bin service_file

  src_dir="$(find "$extract_dir" -maxdepth 3 -type f -name x-ui -perm /111 -printf '%h\n' 2>/dev/null | head -n1)"
  [[ -n "$src_dir" ]] || src_dir="$(find "$extract_dir" -maxdepth 3 -type f -name x-ui -printf '%h\n' 2>/dev/null | head -n1)"
  [[ -n "$src_dir" ]] || wm_fail "Could not locate x-ui binary in release archive"

  wm_info "Installing 3X-UI files from $src_dir"
  rm -rf "$XUI_HOME"
  mkdir -p "$XUI_HOME"
  cp -a "$src_dir"/. "$XUI_HOME"/
  chmod +x "$XUI_RUNTIME_BIN" 2>/dev/null || true
  chmod +x "$XUI_HOME"/bin/* 2>/dev/null || true

  service_file="$(find "$extract_dir" -maxdepth 4 -type f \( -name 'x-ui.service' -o -name 'x-ui.service.debian' \) | head -n1)"
  if [[ -n "$service_file" ]]; then
    cp "$service_file" /etc/systemd/system/x-ui.service
    sed -i "s#ExecStart=.*#ExecStart=${XUI_RUNTIME_BIN}#" /etc/systemd/system/x-ui.service || true
  else
    cat > /etc/systemd/system/x-ui.service <<EOF
[Unit]
Description=3X-UI Service
After=network.target nss-lookup.target

[Service]
User=root
WorkingDirectory=${XUI_HOME}
ExecStart=${XUI_RUNTIME_BIN}
Restart=on-failure
RestartSec=5s
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
  fi

  cat > "$XUI_MANAGER_BIN" <<EOF
#!/usr/bin/env bash
case "\${1:-}" in
  start) systemctl start x-ui ;;
  stop) systemctl stop x-ui ;;
  restart) systemctl restart x-ui ;;
  status) systemctl status x-ui --no-pager ;;
  settings) ${XUI_RUNTIME_BIN} setting -show 2>/dev/null || ${XUI_RUNTIME_BIN} settings 2>/dev/null || systemctl status x-ui --no-pager ;;
  *) ${XUI_RUNTIME_BIN} "\$@" ;;
esac
EOF
  chmod +x "$XUI_MANAGER_BIN"

  echo "$tag" > "$XUI_HOME/.wavemesh-release"
  wm_success "3X-UI release files installed: ${tag}"
}

wm_seed_xui_initial_config() {
  wm_info "Preparing 3X-UI initial configuration"
  mkdir -p /etc/x-ui "$XUI_HOME/bin"

  # Different 3X-UI releases initialize their database on first run. We start once,
  # then apply settings through available CLI commands where supported.
  systemctl daemon-reload
  systemctl enable --now "$XUI_SERVICE" >/dev/null 2>&1 || true
  sleep 3

  if [[ -x "$XUI_RUNTIME_BIN" ]]; then
    "$XUI_RUNTIME_BIN" setting -username "$PANEL_USERNAME" >/dev/null 2>&1 || true
    "$XUI_RUNTIME_BIN" setting -password "$PANEL_PASSWORD" >/dev/null 2>&1 || true
    "$XUI_RUNTIME_BIN" setting -port "$PANEL_PORT" >/dev/null 2>&1 || true
    "$XUI_RUNTIME_BIN" setting -webBasePath "$PANEL_PATH" >/dev/null 2>&1 || true
    "$XUI_RUNTIME_BIN" setting -listenIP "127.0.0.1" >/dev/null 2>&1 || true
    "$XUI_RUNTIME_BIN" setting -webCertFile "" >/dev/null 2>&1 || true
    "$XUI_RUNTIME_BIN" setting -webKeyFile "" >/dev/null 2>&1 || true
  fi

  systemctl restart "$XUI_SERVICE" >/dev/null 2>&1 || true
}

wm_wait_for_xui_service() {
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl enable --now "$XUI_SERVICE" >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
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
  local tag="unknown"
  [[ -f "$XUI_HOME/.wavemesh-release" ]] && tag="$(cat "$XUI_HOME/.wavemesh-release")"
  python3 - <<PY
import json
path = "$WM_CONFIG_JSON"
cfg = json.load(open(path, encoding="utf-8"))
cfg.setdefault("installation", {})["xui"] = {
  "repo": "$XUI_REPO",
  "release": "$tag",
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
  wm_info "Installing 3X-UI from latest stable release"

  if wm_xui_panel_installed && systemctl is-active --quiet "$XUI_SERVICE"; then
    wm_success "3X-UI panel already installed and running"
  else
    local result extract_dir tag
    result="$(wm_download_latest_xui_release)"
    extract_dir="${result%%|*}"
    tag="${result##*|}"
    wm_install_xui_files_from_release "$extract_dir" "$tag"
    wm_seed_xui_initial_config
    wm_wait_for_xui_service || wm_fail "3X-UI service did not become active after release installation"
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
