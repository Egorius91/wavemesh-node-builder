#!/usr/bin/env bash

wm_write_reports() {
  wm_info "Writing reports"
  wm_load_config
  local panel_public_url="https://${DOMAIN}${PANEL_PATH}"
  local subscription_url="https://${DOMAIN}${SUB_PATH}"
  local panel_internal_url="http://127.0.0.1:${PANEL_PORT}${PANEL_PATH}"
  local report_summary node_role node_id exit_count route_count node_status subscription_backend xui_version clients_api native_subscription
  report_summary="$(python3 - "$WM_CONFIG_JSON" "$WM_RUNTIME_JSON" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1],encoding="utf-8"))
try: runtime=json.load(open(sys.argv[2],encoding="utf-8"))
except (FileNotFoundError,json.JSONDecodeError): runtime={}
xui=cfg.get("installation",{}).get("xui",{})
subscription=cfg.get("network",{}).get("subscription",{})
print("\t".join((cfg.get("node",{}).get("role","standalone"),cfg.get("node",{}).get("id",""),str(len(cfg.get("exits",[]))),str(len([r for r in cfg.get("routes",[]) if r.get("kind")=="cascade"])),runtime.get("node_status","unknown"),subscription.get("backend","wavemesh-renderer"),xui.get("release","unknown"),str(bool(xui.get("clients_api",False))).lower(),str(bool(xui.get("native_subscription",False))).lower())))
PY
)"
  IFS=$'\t' read -r node_role node_id exit_count route_count node_status subscription_backend xui_version clients_api native_subscription <<< "$report_summary"

  cat > "$WM_REPORT_TXT" <<EOF
WaveMesh Node Report
====================

Domain:              ${DOMAIN}
Public IP:           ${PUBLIC_IP}
Public HTTPS port:   443
Node name:           ${NODE_NAME}
Node ID:             ${node_id}
Node role:           ${node_role}
Cascade exits:       ${exit_count}
Cascade routes:      ${route_count}
Observed health:     ${node_status}
Web Identity:        ${WEB_IDENTITY_NAME}

Panel public URL:    ${panel_public_url}
Panel internal URL:  ${panel_internal_url}
Panel username:      ${PANEL_USERNAME}
Panel password:      ${PANEL_PASSWORD}
Panel token:         ${PANEL_TOKEN}

Transport:           VLESS + XHTTP + TLS via nginx
XHTTP public path:   ${XHTTP_PATH}
XHTTP local listen:  127.0.0.1:${XHTTP_LOCAL_PORT}
Subscription URL:    ${subscription_url}
Subscription backend:${subscription_backend}
3X-UI version:       ${xui_version}
3X-UI Clients API:   ${clients_api}
Native subscription: ${native_subscription}

Important:
- Public client links must use ${DOMAIN}:443.
- Local ports must never appear in subscription output.
- Open TCP 80 and 443 in provider firewall.
EOF
  chmod 600 "$WM_REPORT_TXT"

  cat > "$WM_REPORT_JSON" <<EOF
{
  "domain": "${DOMAIN}",
  "public_ip": "${PUBLIC_IP}",
  "public_port": 443,
  "node_name": "${NODE_NAME}",
  "node_id": "${node_id}",
  "node_role": "${node_role}",
  "cascade_exits": ${exit_count},
  "cascade_routes": ${route_count},
  "node_status": "${node_status}",
  "web_identity_name": "${WEB_IDENTITY_NAME}",
  "panel_public_url": "${panel_public_url}",
  "panel_internal_url": "${panel_internal_url}",
  "panel_username": "${PANEL_USERNAME}",
  "panel_password": "${PANEL_PASSWORD}",
  "panel_token": "${PANEL_TOKEN}",
  "transport": "vless+xhttp+tls",
  "xhttp_path": "${XHTTP_PATH}",
  "xhttp_local_port": "${XHTTP_LOCAL_PORT}",
  "subscription_url": "${subscription_url}",
  "subscription_backend": "${subscription_backend}",
  "xui": {
    "version": "${xui_version}",
    "clients_api": ${clients_api},
    "native_subscription": ${native_subscription}
  },
  "fingerprint": "${FINGERPRINT}",
  "validation_status": "ok"
}
EOF
  chmod 600 "$WM_REPORT_JSON"
  cp "$WM_REPORT_JSON" "$WM_STATE_DIR/report.json"
  chmod 600 "$WM_STATE_DIR/report.json"
  wm_success "Reports written"
}

wm_print_telegram_bot_connection_info() {
  wm_load_config
  local panel_public_url="https://${DOMAIN}${PANEL_PATH}"

  cat <<EOF

Telegram bot connection
=======================

Panel URL:      ${panel_public_url}
Panel login:    ${PANEL_USERNAME}
Panel password: ${PANEL_PASSWORD}

Use these values when adding this node to the Telegram bot.
EOF
}
