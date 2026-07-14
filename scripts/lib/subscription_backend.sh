#!/usr/bin/env bash

WM_SUBSCRIPTION_BACKEND_TOOL="${WM_SUBSCRIPTION_BACKEND_TOOL:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/subscription_backend.py}"
WM_SUBSCRIPTION_BACKEND_ROLLBACK_FILE="${WM_SUBSCRIPTION_BACKEND_ROLLBACK_FILE:-$WM_STATE_DIR/subscription-backend.rollback}"

wm_subscription_backend_for_config() {
  python3 "$WM_SUBSCRIPTION_BACKEND_TOOL" get --config "${1:-$WM_CONFIG_JSON}"
}

wm_subscription_backend_is_native() {
  [[ "$(wm_subscription_backend_for_config "${1:-$WM_CONFIG_JSON}")" == "xui-native" ]]
}

wm_subscription_xui_db() {
  python3 - "$1" <<'PY'
import json,sys
print(json.load(open(sys.argv[1],encoding="utf-8")).get("installation",{}).get("xui",{}).get("database_path", ""))
PY
}

wm_subscription_apply_xui_settings() {
  local config_file="$1" db
  db="$(wm_subscription_xui_db "$config_file")"
  [[ -n "$db" && -f "$db" ]] || { wm_warn "Configured 3X-UI database is unavailable"; return 1; }
  python3 "$WM_SUBSCRIPTION_BACKEND_TOOL" settings-apply --database "$db" --config "$config_file" >/dev/null || return 1
  systemctl restart x-ui || return 1
  wm_xui_wait_ready || return 1
}

wm_subscription_assert_native_listener() {
  local listeners
  listeners="$(ss -ltnp 2>/dev/null | grep ':2096 ' || true)"
  [[ -n "$listeners" ]] || { wm_warn "3X-UI native subscription listener is not active on 2096"; return 1; }
  grep -q '127\.0\.0\.1:2096' <<< "$listeners" || { wm_warn "3X-UI subscription listener is not bound to 127.0.0.1:2096"; return 1; }
  if grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\*|\[::\]):2096' <<< "$listeners"; then
    wm_warn "3X-UI subscription listener is exposed beyond loopback"
    return 1
  fi
}

wm_subscription_validate_native() {
  local config_file="${1:-$WM_CONFIG_JSON}" sub_id="${2:-}" expected_profiles="${3:-}" db settings domain path nginx_file upstream_file public_file probe local_status public_status content matched=0
  wm_subscription_backend_is_native "$config_file" || { wm_warn "Subscription backend is not xui-native"; return 1; }
  db="$(wm_subscription_xui_db "$config_file")"; [[ -n "$db" && -f "$db" ]] || return 1
  settings="$(python3 "$WM_SUBSCRIPTION_BACKEND_TOOL" settings-read --database "$db")" || return 1
  read -r domain path < <(python3 - "$config_file" <<'PY'
import json,sys
c=json.load(open(sys.argv[1],encoding="utf-8")); print(c["server"]["domain"],c["network"]["subscription"]["path"])
PY
)
  SETTINGS="$settings" DOMAIN_VALUE="$domain" PATH_VALUE="$path" python3 - <<'PY' || return 1
import json,os
s=json.loads(os.environ["SETTINGS"]); domain=os.environ["DOMAIN_VALUE"]; path=os.environ["PATH_VALUE"]
expected={"subEnable":"true","subListen":"127.0.0.1","subPort":"2096","subPath":path,"subDomain":domain,"subURI":f"https://{domain}{path}","subJsonEnable":"false","subClashEnable":"false"}
bad={k:{"expected":v,"actual":s.get(k)} for k,v in expected.items() if str(s.get(k,"")).lower()!=v.lower()}
if bad: raise SystemExit("3X-UI subscription settings differ from desired state: "+json.dumps(bad,sort_keys=True))
PY
  wm_subscription_assert_native_listener || return 1
  nginx_file="${WM_NGINX_MANAGED_CONF:-/etc/nginx/wavemesh-managed-locations.conf}"
  [[ -f "$nginx_file" ]] || return 1
  grep -q '# wavemesh-subscription-backend: xui-native' "$nginx_file" || { wm_warn "Managed nginx config is not in xui-native mode"; return 1; }
  grep -q 'proxy_pass http://127.0.0.1:2096;' "$nginx_file" || return 1
  ! grep -Eq 'try_files[[:space:]]+/sub\.txt|/var/www/wavemesh-sub/users' "$nginx_file" || { wm_warn "Native nginx config still contains WaveMesh static subscription locations"; return 1; }
  if [[ -f "${WM_NGINX_SITE_CONF:-/etc/nginx/sites-available/wavemesh-node.conf}" ]] && grep -Eq 'try_files[[:space:]]+/sub\.txt|proxy_pass[[:space:]]+http://127\.0\.0\.1:2096' "${WM_NGINX_SITE_CONF:-/etc/nginx/sites-available/wavemesh-node.conf}"; then
    wm_warn "Main nginx site still contains a conflicting legacy subscription location"
    return 1
  fi
  upstream_file="$(mktemp)"; public_file="$(mktemp)"
  local probe_candidates=()
  if [[ -n "$sub_id" ]]; then probe_candidates=("${path}${sub_id}" "${path}${sub_id}/"); else probe_candidates=("${path}__wavemesh_native_probe__"); fi
  for probe in "${probe_candidates[@]}"; do
    local_status="$(curl -sS --max-time 10 -H "Host: ${domain}" -H 'X-Forwarded-Proto: https' -H "X-Forwarded-Host: ${domain}" -H 'X-Forwarded-Port: 443' -o "$upstream_file" -w '%{http_code}' "http://127.0.0.1:2096${probe}" 2>/dev/null || true)"
    public_status="$(curl -sSk --max-time 10 -o "$public_file" -w '%{http_code}' "https://${domain}${probe}" 2>/dev/null || true)"
    if [[ "$local_status" =~ ^[1-5][0-9][0-9]$ && "$public_status" == "$local_status" ]] && cmp -s "$upstream_file" "$public_file" && { [[ -z "$sub_id" ]] || [[ "$public_status" == "200" ]]; }; then matched=1; break; fi
  done
  (( matched == 1 )) || { rm -f "$upstream_file" "$public_file"; wm_warn "Public native subscription endpoint does not match the local 3X-UI service"; return 1; }
  if [[ -n "$sub_id" ]]; then
    content="$(cat "$public_file")"
    CONTENT="$content" EXPECTED="$expected_profiles" CONFIG="$config_file" python3 - <<'PY' || { rm -f "$upstream_file" "$public_file"; return 1; }
import base64,json,os,urllib.parse
content=os.environ["CONTENT"].strip()
if "vless://" not in content:
    try: content=base64.b64decode(content).decode()
    except Exception: pass
profiles=[line for line in content.splitlines() if line.startswith("vless://")]
if not profiles: raise SystemExit("native subscription contains no VLESS profiles")
expected=os.environ.get("EXPECTED","")
if expected and len(profiles)!=int(expected): raise SystemExit(f"expected {expected} profiles, received {len(profiles)}")
cfg=json.load(open(os.environ["CONFIG"],encoding="utf-8")); forbidden={"127.0.0.1","localhost",str(cfg.get("server",{}).get("public_ip",""))}
domain=cfg["server"]["domain"]
published=[]
for route in cfg.get("routes",[]):
    visible=route.get("enabled",True) and (route.get("kind")=="cascade" or (route.get("kind")=="auto" and route.get("presentation",{}).get("published",False)))
    if visible: published.append((route.get("entry",{}).get("public_path"),route.get("display_name","")))
allowed_paths={item[0] for item in published}; allowed_names={item[1] for item in published}
for profile in profiles:
    parsed=urllib.parse.urlsplit(profile)
    query=urllib.parse.parse_qs(parsed.query)
    if parsed.hostname!=domain or parsed.port!=443: raise SystemExit("native profile does not use the Entry domain on port 443")
    if query.get("type")!=["xhttp"] or query.get("security")!=["tls"]: raise SystemExit("native profile is not VLESS XHTTP TLS")
    if query.get("host")!=[domain] or query.get("sni")!=[domain]: raise SystemExit("native profile host or SNI differs from the Entry domain")
    if allowed_paths and query.get("path",[""])[0] not in allowed_paths: raise SystemExit("native profile exposes an unpublished or unknown path")
    if allowed_names and urllib.parse.unquote(parsed.fragment) not in allowed_names: raise SystemExit("native profile exposes an unpublished or unknown display name")
for item in cfg.get("exits",[]):
    endpoint=item.get("endpoint",{}); forbidden.update(map(str,[endpoint.get("domain",""),endpoint.get("relay_uuid",""),endpoint.get("relay_path","")]+endpoint.get("expected_public_ips",[])))
for value in forbidden:
    if value and value in content: raise SystemExit("native subscription leaks an internal or Exit value")
PY
  fi
  rm -f "$upstream_file" "$public_file"
}

wm_subscription_backend_status() {
  wm_load_config
  local backend db settings nginx_file files_present locations_present detected consistency="ok"
  backend="$(wm_subscription_backend_for_config)"; db="$(wm_subscription_xui_db "$WM_CONFIG_JSON")"
  settings='{}'; [[ -n "$db" && -f "$db" ]] && settings="$(python3 "$WM_SUBSCRIPTION_BACKEND_TOOL" settings-read --database "$db" 2>/dev/null || printf '{}')"
  nginx_file="${WM_NGINX_MANAGED_CONF:-/etc/nginx/wavemesh-managed-locations.conf}"
  detected="unknown"; [[ -f "$nginx_file" ]] && grep -q 'wavemesh-subscription-backend: xui-native' "$nginx_file" && detected="xui-native"; [[ -f "$nginx_file" ]] && grep -q 'wavemesh-subscription-backend: wavemesh-renderer' "$nginx_file" && detected="wavemesh-renderer"
  [[ -f "$WM_SUB_DIR/sub.txt" || -d "$WM_SUB_DIR/users" ]] && files_present=true || files_present=false
  [[ -f "$nginx_file" ]] && grep -q '/var/www/wavemesh-sub/users' "$nginx_file" && locations_present=true || locations_present=false
  [[ "$detected" == "$backend" ]] || consistency="mismatch"
  SETTINGS="$settings" CONFIG_FILE="$WM_CONFIG_JSON" python3 - "$backend" "$detected" "$files_present" "$locations_present" "$consistency" <<'PY'
import json,os,sys
s=json.loads(os.environ["SETTINGS"])
cfg=json.load(open(os.environ["CONFIG_FILE"],encoding="utf-8")); sub=cfg["network"]["subscription"]
expected_enable="true" if sys.argv[1]=="xui-native" else "false"
consistent=sys.argv[5]
if str(s.get("subEnable","")).lower()!=expected_enable or s.get("subPath")!=sub.get("path") or s.get("subDomain")!=cfg["server"]["domain"]: consistent="mismatch"
print(f"Configured backend: {sys.argv[1]}")
print(f"Detected nginx backend: {sys.argv[2]}")
print(f"3X-UI subEnable: {s.get('subEnable','unknown')}")
print(f"3X-UI subPath: {s.get('subPath','unknown')}")
print(f"3X-UI subDomain: {s.get('subDomain','unknown')}")
print(f"3X-UI listener: {s.get('subListen','unknown')}:{s.get('subPort','unknown')}")
print(f"WaveMesh subscription files present: {sys.argv[3]}")
print(f"Managed per-client locations present: {sys.argv[4]}")
print(f"Configuration consistency: {consistent}")
PY
}

wm_subscription_backend_switch() {
  local target="${1:-}" mode="" current transaction candidate effective prepared metadata backup path
  shift || true
  while [[ $# -gt 0 ]]; do
    case "$1" in --dry-run) mode="dry-run"; shift ;; --apply) mode="apply"; shift ;; *) wm_fail "Usage: wavemesh subscription backend switch xui-native|wavemesh-renderer --dry-run|--apply" ;; esac
  done
  [[ "$target" == "xui-native" || "$target" == "wavemesh-renderer" ]] || wm_fail "Unsupported subscription backend: ${target}"
  [[ -n "$mode" ]] || wm_fail "Backend switch requires --dry-run or --apply"
  wm_load_config; current="$(wm_subscription_backend_for_config)"; candidate="$(mktemp)"
  python3 "$WM_SUBSCRIPTION_BACKEND_TOOL" set --config "$WM_CONFIG_JSON" --output "$candidate" --backend "$target" || { rm -f "$candidate"; wm_fail "Could not prepare backend candidate"; }
  path="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["network"]["subscription"]["path"])' "$candidate")"
  if [[ "$mode" == "dry-run" ]]; then
    wm_info "Configured backend: ${current}"
    wm_info "Target backend: ${target}"
    wm_info "Files to update: ${WM_CONFIG_JSON}, ${WM_STATE_DIR}/config.env, configured x-ui.db, ${WM_NGINX_SITE_CONF}, ${WM_NGINX_MANAGED_CONF}"
    wm_info "Files preserved: ${WM_SUB_DIR}/ (existing renderer output is backed up and left on disk)"
    if [[ "$target" == "xui-native" ]]; then
      wm_info "nginx plan: remove legacy static/per-client subscription locations and add one native namespace proxy to 127.0.0.1:2096"
    else
      wm_info "nginx plan: remove the native namespace proxy and restore managed per-client renderer locations"
    fi
    wm_info "Candidate 3X-UI settings: subEnable=$([[ "$target" == "xui-native" ]] && echo true || echo false), subPath=${path}"
    wm_info "Active URL after apply: https://${DOMAIN}${path}<subId>"
    [[ "$current" == "$target" ]] || wm_warn "Legacy URLs served only by ${current} will stop being published after a successful apply"
    rm -f "$candidate"
    return 0
  fi
  rm -f "$candidate"
  [[ "$current" != "$target" ]] || { wm_info "Subscription backend is already ${target}"; return 0; }
  wm_lock_mutation "subscription-backend-switch"; wm_transaction_begin "subscription-backend-switch"; transaction="$WM_ACTIVE_TRANSACTION"
  candidate="$transaction/config.backend.json"; effective="$transaction/config.effective.json"; prepared="$transaction/subscriptions"; metadata="$transaction/subscriptions.json"; backup="$transaction/subscriptions.render.before"
  python3 "$WM_SUBSCRIPTION_BACKEND_TOOL" set --config "$WM_CONFIG_JSON" --output "$candidate" --backend "$target" || wm_fail "Could not prepare backend candidate"
  wm_subscription_apply_xui_settings "$candidate" || wm_fail "Could not apply 3X-UI subscription settings"
  if [[ "$target" == "xui-native" ]]; then
    wm_xui_probe_clients_api || wm_fail "3X-UI Clients API is unavailable"
    wm_subscription_assert_native_listener || wm_fail "Native subscription listener failed verification"
    cp "$candidate" "$effective"; printf '[]\n' > "$metadata"; mkdir -p "$prepared"
  else
    mkdir -p "$prepared" "$backup"
    wm_subscription_prepare "$candidate" "$effective" "$prepared" "$metadata" || wm_fail "Could not render fallback subscriptions"
    wm_subscription_install_files "$prepared" "$backup"
  fi
  wm_nginx_sanitize_subscription_site "$effective" "$transaction" || wm_fail "Could not remove legacy subscription locations from nginx site"
  wm_nginx_apply_desired "$effective" "$transaction" || wm_fail "nginx rejected subscription backend candidate"
  if [[ "$target" == "xui-native" ]]; then
    wm_subscription_validate_native "$effective" || wm_fail "Native subscription validation failed"
  else
    wm_subscription_validate_public "$metadata" || wm_fail "WaveMesh renderer validation failed"
  fi
  wm_atomic_install_json "$effective" "$WM_CONFIG_JSON"; wm_export_config_env_from_json
  wm_transaction_commit "$transaction"
  printf '%s\n' "$(basename "$transaction")" > "$WM_SUBSCRIPTION_BACKEND_ROLLBACK_FILE"; chmod 600 "$WM_SUBSCRIPTION_BACKEND_ROLLBACK_FILE"
  wm_success "Subscription backend switched to ${target}"
  wm_info "Rollback is available with: wavemesh subscription backend rollback"
}

wm_subscription_backend_rollback() {
  local transaction_id transaction
  [[ -f "$WM_SUBSCRIPTION_BACKEND_ROLLBACK_FILE" ]] || wm_fail "No subscription backend switch is available for rollback"
  transaction_id="$(cat "$WM_SUBSCRIPTION_BACKEND_ROLLBACK_FILE")"
  [[ "$transaction_id" =~ ^[0-9TZ-]+[a-f0-9]{6}$ ]] || wm_fail "Stored backend rollback id is invalid"
  transaction="$WM_TRANSACTION_ROOT/$transaction_id"
  [[ -d "$transaction" && -f "$transaction/config.before.json" ]] || wm_fail "Backend rollback snapshot is unavailable"
  wm_lock_mutation "subscription-backend-rollback"
  wm_transaction_rollback "$transaction" "subscription backend rollback" || wm_fail "Subscription backend rollback failed"
  rm -f "$WM_SUBSCRIPTION_BACKEND_ROLLBACK_FILE"
  wm_success "Subscription backend rollback completed"
}

wm_subscription_backend_command() {
  case "${1:-}" in
    status) shift; wm_subscription_backend_status "$@" ;;
    switch) shift; wm_subscription_backend_switch "$@" ;;
    rollback) shift; wm_subscription_backend_rollback "$@" ;;
    *) wm_fail "Usage: wavemesh subscription backend status|switch BACKEND --dry-run|--apply|rollback" ;;
  esac
}

wm_subscription_validate_native_command() {
  local sub_id="" expected=""
  while [[ $# -gt 0 ]]; do case "$1" in --sub-id) sub_id="${2:-}"; shift 2 ;; --expected-profiles) expected="${2:-}"; shift 2 ;; *) wm_fail "Usage: wavemesh subscription validate-native [--sub-id ID --expected-profiles N]" ;; esac; done
  [[ -z "$expected" || "$expected" =~ ^[0-9]+$ ]] || wm_fail "--expected-profiles must be a non-negative integer"
  [[ -z "$expected" || -n "$sub_id" ]] || wm_fail "--expected-profiles requires --sub-id"
  wm_load_config; wm_subscription_validate_native "$WM_CONFIG_JSON" "$sub_id" "$expected" || wm_fail "Native subscription validation failed"
  wm_success "Native 3X-UI subscription backend is valid"
}
