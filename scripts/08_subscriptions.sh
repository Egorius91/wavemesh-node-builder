#!/usr/bin/env bash

wm_urlencode_path() {
  python3 - <<PY
import urllib.parse
print(urllib.parse.quote('${XHTTP_PATH}', safe=''))
PY
}

wm_generate_fallback_subscription() {
  wm_info "Generating fallback subscription"
  mkdir -p "$WM_SUB_DIR"
  wm_load_config
  local encoded_path
  encoded_path="$(wm_urlencode_path)"
  : > "$WM_SUB_DIR/sub.txt"
  IFS=',' read -ra UUIDS <<< "$CLIENT_UUIDS"
  local i=1
  for uuid in "${UUIDS[@]}"; do
    echo "vless://${uuid}@${DOMAIN}:443?type=xhttp&security=tls&path=${encoded_path}&host=${DOMAIN}&sni=${DOMAIN}&fp=${FINGERPRINT}&mode=stream-one&encryption=none#${NODE_NAME}-${i}" >> "$WM_SUB_DIR/sub.txt"
    i=$((i+1))
  done
  base64 -w0 "$WM_SUB_DIR/sub.txt" > "$WM_SUB_DIR/sub.b64" || true
  chmod 644 "$WM_SUB_DIR/sub.txt" "$WM_SUB_DIR/sub.b64"
  wm_success "Fallback subscription generated: https://${DOMAIN}${SUB_PATH}"
}

wm_validate_subscription_output() {
  wm_info "Validating subscription output"
  local content decoded source_url
  source_url="https://${DOMAIN}${SUB_PATH}"
  content="$(cat "$WM_SUB_DIR/sub.txt" 2>/dev/null || true)"
  [[ -n "$content" ]] || wm_fail "Subscription file is empty"

  decoded="$content"
  if ! grep -q '^vless://' <<< "$decoded"; then
    decoded="$(echo "$content" | base64 -d 2>/dev/null || true)"
  fi

  [[ "$decoded" == *"vless://"* ]] || wm_fail "No vless:// links found in subscription"

  local forbidden=("127.0.0.1" "localhost" "${XHTTP_LOCAL_PORT}" "${PANEL_PORT}")
  if [[ -n "${PUBLIC_IP:-}" ]]; then forbidden+=("${PUBLIC_IP}"); fi
  for bad in "${forbidden[@]}"; do
    if [[ -n "$bad" ]] && grep -q "$bad" <<< "$decoded"; then
      wm_fail "Subscription exposes forbidden value: $bad"
    fi
  done

  grep -q "@${DOMAIN}:443" <<< "$decoded" || wm_fail "Subscription does not use ${DOMAIN}:443"
  grep -q "type=xhttp" <<< "$decoded" || wm_fail "Subscription does not include type=xhttp"
  grep -q "mode=stream-one" <<< "$decoded" || wm_fail "Subscription does not include mode=stream-one"
  grep -q "security=tls" <<< "$decoded" || wm_fail "Subscription does not include security=tls"
  grep -q "sni=${DOMAIN}" <<< "$decoded" || wm_fail "Subscription does not include sni=${DOMAIN}"
  grep -q "host=${DOMAIN}" <<< "$decoded" || wm_fail "Subscription does not include host=${DOMAIN}"
  grep -q "encryption=none" <<< "$decoded" || wm_fail "Subscription does not include encryption=none"

  local public_content
  public_content="$(curl -fsSk --max-time 10 "$source_url" 2>/dev/null || true)"
  [[ -n "$public_content" ]] || wm_fail "Public subscription URL is not reachable: ${source_url}"
  [[ "$public_content" == "$content" ]] || wm_fail "Public subscription content does not match generated file"

  wm_success "Subscription is valid: ${source_url}"
}
