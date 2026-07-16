#!/usr/bin/env bash

WM_NATIVE_SUBSCRIPTION_TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/native_subscription.py"

wm_subscription_backend() {
  local config="${1:-$WM_CONFIG_JSON}"
  python3 - "$config" <<'PY'
import json,sys
item=json.load(open(sys.argv[1],encoding="utf-8")).get("network",{}).get("subscription",{})
print(item.get("backend") or item.get("mode") or "generated")
PY
}

wm_native_settings_endpoint() {
  local response_file="$1"
  if wm_xui_request_success POST /panel/api/setting/all none > "$response_file"; then
    printf '%s\n' /panel/api/setting
    return 0
  fi
  if wm_xui_request_success POST /panel/setting/all none > "$response_file"; then
    printf '%s\n' /panel/setting
    return 0
  fi
  return 1
}

wm_native_apply_settings() {
  local config="${1:-$WM_CONFIG_JSON}" response current payload endpoint readback
  response="$(mktemp)"; current="$(mktemp)"; payload="$(mktemp)"; readback="$(mktemp)"
  endpoint="$(wm_native_settings_endpoint "$response")" || { rm -f "$response" "$current" "$payload" "$readback"; wm_warn "3X-UI settings API is unavailable"; return 1; }
  python3 - "$response" "$current" <<'PY' || { rm -f "$response" "$current" "$payload" "$readback"; return 1; }
import json,sys
data=json.load(open(sys.argv[1],encoding="utf-8")); obj=data.get("obj")
if not isinstance(obj,dict): raise SystemExit("3X-UI settings response has no obj object")
json.dump(obj,open(sys.argv[2],"w",encoding="utf-8"),indent=2,ensure_ascii=False)
PY
  python3 "$WM_NATIVE_SUBSCRIPTION_TOOL" settings --config "$config" --current "$current" --output "$payload" || { rm -f "$response" "$current" "$payload" "$readback"; return 1; }
  if python3 - "$current" "$payload" <<'PY'
import json,sys
raise SystemExit(0 if json.load(open(sys.argv[1],encoding="utf-8")) == json.load(open(sys.argv[2],encoding="utf-8")) else 1)
PY
  then
    rm -f "$response" "$current" "$payload" "$readback"
    return 0
  fi
  wm_xui_request_success POST "${endpoint}/update" json "$(cat "$payload")" >/dev/null || { rm -f "$response" "$current" "$payload" "$readback"; return 1; }
  systemctl restart x-ui || { rm -f "$response" "$current" "$payload" "$readback"; return 1; }
  wm_xui_wait_ready || { rm -f "$response" "$current" "$payload" "$readback"; return 1; }
  wm_native_settings_endpoint "$readback" >/dev/null || { rm -f "$response" "$current" "$payload" "$readback"; return 1; }
  CONFIG="$config" python3 - "$readback" <<'PY' || { rm -f "$response" "$current" "$payload" "$readback"; return 1; }
import json,os,sys
cfg=json.load(open(os.environ["CONFIG"],encoding="utf-8")); data=json.load(open(sys.argv[1],encoding="utf-8")).get("obj",{})
expected={"subEnable":True,"subListen":"127.0.0.1","subPort":2096,"subPath":cfg["network"]["subscription"]["path"],"subDomain":cfg["server"]["domain"],"remarkTemplate":"{{INBOUND}}"}
for key,value in expected.items():
    actual=data.get(key)
    if str(actual).lower()!=str(value).lower(): raise SystemExit(f"3X-UI setting read-back mismatch: {key}")
PY
  rm -f "$response" "$current" "$payload" "$readback"
}

wm_native_capabilities_json() {
  local config="${1:-$WM_CONFIG_JSON}" openapi settings_response settings inbounds api_caps listener backend custom_locations
  openapi="$(mktemp)"; settings_response="$(mktemp)"; settings="$(mktemp)"; inbounds="$(mktemp)"
  wm_xui_request GET /panel/api/openapi.json none > "$openapi" || printf '{}\n' > "$openapi"
  api_caps="$(python3 "$WM_NATIVE_SUBSCRIPTION_TOOL" capabilities --openapi "$openapi")" || api_caps='{}'
  if wm_native_settings_endpoint "$settings_response" >/dev/null; then
    python3 - "$settings_response" "$settings" <<'PY'
import json,sys
json.dump(json.load(open(sys.argv[1],encoding="utf-8")).get("obj",{}),open(sys.argv[2],"w",encoding="utf-8"))
PY
  else
    printf '{}\n' > "$settings"
  fi
  wm_xui_request_success GET /panel/api/inbounds/list none > "$inbounds" || printf '{"obj":[]}\n' > "$inbounds"
  listener=false
  if ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq '^127\.0\.0\.1:2096$'; then listener=true; fi
  backend="$(wm_subscription_backend "$config")"
  custom_locations=false
  if [[ -f "${WM_NGINX_MANAGED_CONF:-/etc/nginx/wavemesh-managed-locations.conf}" ]] && grep -Fq 'root /var/www/wavemesh-sub/users;' "${WM_NGINX_MANAGED_CONF:-/etc/nginx/wavemesh-managed-locations.conf}"; then custom_locations=true; fi
  API_CAPS="$api_caps" LISTENER="$listener" BACKEND="$backend" CUSTOM_LOCATIONS="$custom_locations" CONFIG="$config" python3 - "$settings" "$inbounds" "$WM_NATIVE_SUBSCRIPTION_TOOL" <<'PY'
import importlib.util,json,os,sys
spec=importlib.util.spec_from_file_location("native",sys.argv[3]); native=importlib.util.module_from_spec(spec); spec.loader.exec_module(native)
settings=json.load(open(sys.argv[1],encoding="utf-8")); inbounds=json.load(open(sys.argv[2],encoding="utf-8")); cfg=json.load(open(os.environ["CONFIG"],encoding="utf-8"))
api=json.loads(os.environ["API_CAPS"]); view=native.visible_inbounds(inbounds); expected=cfg["network"]["subscription"]
result={
 "backend":os.environ["BACKEND"], **api,
 "native_listener_loopback":os.environ["LISTENER"]=="true",
 "custom_renderer_locations":os.environ["CUSTOM_LOCATIONS"]=="true",
 "sub_enable":str(settings.get("subEnable","")).lower()=="true",
 "sub_listen":settings.get("subListen"), "sub_port":int(settings.get("subPort") or 0),
 "local_port_matches":int(settings.get("subPort") or 0)==int(expected.get("local_port") or 2096),
 "sub_path_matches":settings.get("subPath")==expected.get("path"),
 "remark_template_matches":settings.get("remarkTemplate")=="{{INBOUND}}",
 "public_inbounds":len(view["public"]), "hidden_inbounds":len(view["hidden"]),
 "rejected_enabled_inbounds":len(view["rejected"]),
}
result["ready"]=all((result.get("clients_api"),result.get("settings_api"),result.get("inbounds_api"),result["native_listener_loopback"],result["sub_enable"],result["sub_listen"]=="127.0.0.1",result["sub_port"]==2096,result["local_port_matches"],result["sub_path_matches"],result["remark_template_matches"],not result["custom_renderer_locations"]))
print(json.dumps(result,indent=2,ensure_ascii=False,sort_keys=True))
PY
  rm -f "$openapi" "$settings_response" "$settings" "$inbounds"
}

wm_native_require_capabilities() {
  local config="${1:-$WM_CONFIG_JSON}" allow_custom_renderer="${2:-false}" report
  report="$(wm_native_capabilities_json "$config")" || return 1
  printf '%s' "$report" | python3 -c '
import json, sys

report = json.load(sys.stdin)
allow_custom_renderer = sys.argv[1].lower() == "true"
required = (
    report.get("clients_api"),
    report.get("settings_api"),
    report.get("inbounds_api"),
    report.get("native_listener_loopback"),
    report.get("sub_enable"),
    report.get("sub_listen") == "127.0.0.1",
    report.get("sub_port") == 2096,
    report.get("local_port_matches"),
    report.get("sub_path_matches"),
    report.get("remark_template_matches"),
    allow_custom_renderer or not report.get("custom_renderer_locations"),
)
raise SystemExit(0 if all(required) else 1)
' "$allow_custom_renderer" || { wm_warn "3X-UI native subscription capabilities are incomplete"; return 1; }
}

wm_native_validate_profile_counts() {
  local config="${1:-$WM_CONFIG_JSON}" sub_id links expected_api expected_config checked=0
  while IFS= read -r sub_id; do
    [[ -n "$sub_id" ]] || continue
    links="$(mktemp)"
    wm_xui_request_success GET "/panel/api/clients/subLinks/${sub_id}" none > "$links" || { rm -f "$links"; return 1; }
    expected_api="$(python3 - "$links" <<'PY'
import json,sys
obj=json.load(open(sys.argv[1],encoding="utf-8")).get("obj",[])
print(len(obj) if isinstance(obj,list) else -1)
PY
)"
    expected_config="$(python3 "$WM_NATIVE_SUBSCRIPTION_TOOL" expected-profiles --config "$config" --subscription-id "$sub_id")" || { rm -f "$links"; return 1; }
    rm -f "$links"
    (( expected_config > 0 )) || { wm_warn "Canonical config has no published profiles for an enabled builder client"; return 1; }
    [[ "$expected_api" == "$expected_config" ]] || { wm_warn "Clients API profile count differs from canonical published routes: ${expected_api} != ${expected_config}"; return 1; }
    checked=$((checked+1))
  done < <(python3 - "$config" <<'PY'
import json,sys
for client in json.load(open(sys.argv[1],encoding="utf-8")).get("clients",[]):
    if client.get("enabled",True) and client.get("subscription_id"): print(client["subscription_id"])
PY
)
  (( checked > 0 )) || wm_warn "No builder-managed clients are available for profile count validation"
}

wm_native_fetch_public() {
  local url="$1" output="$2" attempt
  for attempt in $(seq 1 10); do
    if curl -fsSk --max-time 10 -H 'Accept: text/plain' "$url" -o "$output"; then
      return 0
    fi
    (( attempt < 10 )) && sleep 1
  done
  return 1
}

wm_native_validate_public() {
  local config="${1:-$WM_CONFIG_JSON}" allow_custom_renderer="${2:-false}" sub_id content links expected expected_config path forbidden validated=0
  wm_native_require_capabilities "$config" "$allow_custom_renderer" || return 1
  forbidden="127.0.0.1,localhost,$PUBLIC_IP,$PANEL_PORT,$XHTTP_LOCAL_PORT,2096"
  while IFS= read -r sub_id; do
    [[ -n "$sub_id" ]] || continue
    links="$(mktemp)"; content="$(mktemp)"
    wm_xui_request_success GET "/panel/api/clients/subLinks/${sub_id}" none > "$links" || { rm -f "$links" "$content"; return 1; }
    expected="$(python3 - "$links" <<'PY'
import json,sys
obj=json.load(open(sys.argv[1],encoding="utf-8")).get("obj",[])
print(len(obj) if isinstance(obj,list) else -1)
PY
)"
    (( expected > 0 )) || { rm -f "$links" "$content"; wm_warn "Configured client has no enabled native subscription profiles"; return 1; }
    expected_config="$(python3 "$WM_NATIVE_SUBSCRIPTION_TOOL" expected-profiles --config "$config" --subscription-id "$sub_id")" || { rm -f "$links" "$content"; return 1; }
    [[ "$expected" == "$expected_config" ]] || { rm -f "$links" "$content"; wm_warn "Native profile count differs from canonical published routes: ${expected} != ${expected_config}"; return 1; }
    path="$(python3 - "$config" "$sub_id" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1],encoding="utf-8")); print(cfg["network"]["subscription"]["path"]+sys.argv[2])
PY
)"
    wm_native_fetch_public "https://${DOMAIN}${path}" "$content" || { rm -f "$links" "$content"; wm_warn "Native public subscription URL is unreachable after readiness retries"; return 1; }
    python3 "$WM_NATIVE_SUBSCRIPTION_TOOL" validate-content --content "$content" --domain "$DOMAIN" --forbidden "$forbidden" --expected-profiles "$expected" >/dev/null || { rm -f "$links" "$content"; return 1; }
    rm -f "$links" "$content"; validated=$((validated+1))
  done < <(python3 - "$config" <<'PY'
import json,sys
for client in json.load(open(sys.argv[1],encoding="utf-8")).get("clients",[]):
    if client.get("enabled",True) and client.get("subscription_id"): print(client["subscription_id"])
PY
)
  (( validated > 0 )) || wm_warn "No builder-managed clients are available for public content validation"
}

wm_native_reconcile_builder_subids() {
  local config="$1" output="$2" clients actions action email encoded sub_id current payload verify
  clients="$(mktemp)"; actions="$(mktemp)"; current="$(mktemp)"; payload="$(mktemp)"; verify="$(mktemp)"
  wm_xui_request_success GET /panel/api/clients/list none > "$clients" || { rm -f "$clients" "$actions" "$current" "$payload" "$verify"; return 1; }
  python3 "$WM_NATIVE_SUBSCRIPTION_TOOL" client-plan --config "$config" --clients "$clients" --output-config "$output" --actions "$actions" || { rm -f "$clients" "$actions" "$current" "$payload" "$verify"; return 1; }
  while IFS= read -r action; do
    email="$(printf '%s' "$action" | python3 -c 'import json,sys; print(json.load(sys.stdin)["email"])')"
    sub_id="$(printf '%s' "$action" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sub_id"])')"
    encoded="$(EMAIL="$email" python3 -c 'import os,urllib.parse; print(urllib.parse.quote(os.environ["EMAIL"],safe=""))')"
    wm_xui_request_success GET "/panel/api/clients/get/${encoded}" none > "$current" || { rm -f "$clients" "$actions" "$current" "$payload" "$verify"; return 1; }
    SUB_ID="$sub_id" python3 - "$current" "$payload" <<'PY' || { rm -f "$clients" "$actions" "$current" "$payload" "$verify"; return 1; }
import json,os,sys
obj=json.load(open(sys.argv[1],encoding="utf-8")).get("obj")
if not isinstance(obj,dict): raise SystemExit("3X-UI client response has no obj object")
for key in ("id","inboundIds","traffic","externalLinks"): obj.pop(key,None)
obj["subId"]=os.environ["SUB_ID"]
json.dump(obj,open(sys.argv[2],"w",encoding="utf-8"),separators=(",",":"))
PY
    wm_xui_request_success POST "/panel/api/clients/update/${encoded}" json "$(cat "$payload")" >/dev/null || { rm -f "$clients" "$actions" "$current" "$payload" "$verify"; return 1; }
  done < <(python3 -c 'import json,sys; [print(json.dumps(x,separators=(",",":"))) for x in json.load(open(sys.argv[1]))]' "$actions")
  wm_xui_request_success GET /panel/api/clients/list none > "$clients" || { rm -f "$clients" "$actions" "$current" "$payload" "$verify"; return 1; }
  python3 "$WM_NATIVE_SUBSCRIPTION_TOOL" client-plan --config "$output" --clients "$clients" --output-config "$verify" --actions "$actions" || { rm -f "$clients" "$actions" "$current" "$payload" "$verify"; return 1; }
  python3 - "$actions" <<'PY' || { rm -f "$clients" "$actions" "$current" "$payload" "$verify"; return 1; }
import json,sys
if json.load(open(sys.argv[1],encoding="utf-8")): raise SystemExit("native client subscription ID read-back still requires changes")
PY
  cp "$verify" "$output"
  rm -f "$clients" "$actions" "$current" "$payload" "$verify"
}
