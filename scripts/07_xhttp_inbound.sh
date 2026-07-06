#!/usr/bin/env bash

wm_create_xhttp_inbound() {
  wm_info "Creating VLESS + XHTTP inbound placeholder"
  wm_warn "MVP skeleton: real 3X-UI API/database inbound creation will be added next."
  wm_info "Required final inbound: listen 127.0.0.1:${XHTTP_LOCAL_PORT}, transport xhttp, path ${XHTTP_PATH}, inbound security none."
  wm_success "XHTTP inbound step completed as placeholder"
}

wm_create_clients() {
  wm_info "Creating ${CLIENT_COUNT} fallback client UUID(s)"
  local uuids=()
  for _ in $(seq 1 "$CLIENT_COUNT"); do
    uuids+=("$(cat /proc/sys/kernel/random/uuid)")
  done
  CLIENT_UUIDS="$(IFS=,; echo "${uuids[*]}")"
  echo "CLIENT_UUIDS=\"${CLIENT_UUIDS}\"" >> "$WM_STATE_DIR/config.env"
  wm_success "Fallback clients generated"
}
