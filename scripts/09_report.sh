#!/usr/bin/env bash

wm_write_reports() {
  wm_info "Writing reports"
  wm_load_config
  local subscription_url="https://${DOMAIN}${SUB_PATH}"
  local panel_url="https://${DOMAIN}:${PANEL_PORT}${PANEL_PATH}"

  cat > "$WM_REPORT_TXT" <<EOF
WaveMesh Node Report
====================

Domain:              ${DOMAIN}
Public IP:           ${PUBLIC_IP}
Public HTTPS port:   443
Node name:           ${NODE_NAME}
Web Identity:        ${WEB_IDENTITY_NAME}

Panel URL:           ${panel_url}
Panel username:      ${PANEL_USERNAME}
Panel password:      ${PANEL_PASSWORD}
Panel token:         ${PANEL_TOKEN}

Transport:           VLESS + XHTTP + TLS via nginx
XHTTP public path:   ${XHTTP_PATH}
XHTTP local listen:  127.0.0.1:${XHTTP_LOCAL_PORT}
Subscription URL:    ${subscription_url}
Subscription mode:   fallback-generated static file

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
  "web_identity_name": "${WEB_IDENTITY_NAME}",
  "panel_url": "${panel_url}",
  "panel_username": "${PANEL_USERNAME}",
  "panel_password": "${PANEL_PASSWORD}",
  "panel_token": "${PANEL_TOKEN}",
  "transport": "vless+xhttp+tls",
  "xhttp_path": "${XHTTP_PATH}",
  "xhttp_local_port": "${XHTTP_LOCAL_PORT}",
  "subscription_url": "${subscription_url}",
  "subscription_mode": "fallback-generated",
  "fingerprint": "${FINGERPRINT}",
  "validation_status": "ok"
}
EOF
  chmod 600 "$WM_REPORT_JSON"
  cp "$WM_REPORT_JSON" "$WM_STATE_DIR/report.json"
  chmod 600 "$WM_STATE_DIR/report.json"
  wm_success "Reports written"
}
