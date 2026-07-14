#!/usr/bin/env bash

wm_run_diagnostics() {
  wm_info "Running diagnostics"
  wm_load_config
  local ok=1

  systemctl is-active --quiet nginx && wm_success "nginx active" || { wm_warn "nginx not active"; ok=0; }
  [[ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]] && wm_success "SSL certificate exists" || { wm_warn "SSL certificate missing"; ok=0; }
  [[ -f "$WM_SITE_DIR/index.html" ]] && wm_success "Web Identity exists" || { wm_warn "Web Identity missing"; ok=0; }
  curl -fsSk --max-time 10 "https://${DOMAIN}${PANEL_PATH}" >/dev/null 2>&1 && wm_success "Public panel URL reachable" || { wm_warn "Public panel URL not reachable"; ok=0; }

  if [[ "$SUB_BACKEND" == "xui-native" ]]; then
    if wm_native_capabilities_json | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("ready") else 1)'; then
      wm_success "3X-UI native subscription capabilities ready"
    else
      wm_warn "3X-UI native subscription capabilities incomplete"
      ok=0
    fi
    if wm_native_validate_public; then
      wm_success "Native public subscription validated"
    else
      wm_warn "Native public subscription validation failed"
      ok=0
    fi
  else
    [[ -f "$WM_SUB_DIR/sub.txt" ]] && wm_success "Subscription file exists" || { wm_warn "Subscription file missing"; ok=0; }
    curl -fsSk --max-time 10 "https://${DOMAIN}${SUB_PATH}" >/dev/null 2>&1 && wm_success "Public subscription URL reachable" || { wm_warn "Public subscription URL not reachable"; ok=0; }

    if grep -q "${XHTTP_LOCAL_PORT}" "$WM_SUB_DIR/sub.txt" 2>/dev/null; then
      wm_warn "Subscription leaks local XHTTP port"
      ok=0
    fi
  fi

  if (( ok == 1 )); then
    wm_success "Diagnostics OK"
  else
    wm_warn "Diagnostics completed with warnings"
  fi
}
