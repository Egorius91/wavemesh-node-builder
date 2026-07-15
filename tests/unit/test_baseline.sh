#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

python3 - "$ROOT_DIR/scripts/07_xhttp_inbound.sh" <<'PY'
import ast
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
match = re.search(r'xui\["inbound"\]\s*=\s*({.*?})\nwith open', text, re.S)
if not match:
    raise SystemExit("inbound config object not found")

tree = ast.parse(match.group(1), mode="eval")
keys = [node.value for node in tree.body.keys if isinstance(node, ast.Constant)]
if len(keys) != len(set(keys)):
    raise SystemExit("duplicate keys in inbound config object")
if "creation_mode" not in keys or "xhttp_mode" not in keys:
    raise SystemExit("inbound config must expose creation_mode and xhttp_mode")
PY

for file in "$ROOT_DIR/install.sh" "$ROOT_DIR/bin/wavemesh" "$ROOT_DIR"/scripts/*.sh; do
  bash -n "$file"
done

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/transaction"
cat > "$tmp/native.json" <<'JSON'
{"server":{"domain":"entry.example.com"},"network":{"xhttp":{"path":"/api/public-path/"},"subscription":{"path":"/new-native-path/","backend":"xui-native","mode":"xui-native","local_port":2096}},"panel":{"path":"/panel-path/"},"clients":[],"routes":[],"relay_peers":[]}
JSON
cat > "$tmp/generated.json" <<'JSON'
{"server":{"domain":"entry.example.com"},"network":{"xhttp":{"path":"/api/public-path/"},"subscription":{"path":"/legacy-generated-path/","backend":"generated","mode":"generated","local_port":2096}},"panel":{"path":"/panel-path/"},"clients":[],"routes":[],"relay_peers":[]}
JSON

# Exercise every nginx helper under nounset; declarations must not reference
# another local from the same statement before Bash assigns it.
source "$ROOT_DIR/scripts/lib/nginx_renderer.sh"
WM_NGINX_MANAGED_CONF="$tmp/managed.conf"
nginx() { return 0; }
systemctl() { return 0; }
wm_warn() { return 0; }
wm_nginx_apply_desired "$tmp/native.json" "$tmp/transaction"
wm_nginx_apply_native_migration_candidate "$tmp/generated.json" "$tmp/native.json" "$tmp/transaction"
wm_nginx_apply_native_rotation_candidate "$tmp/native.json" "$tmp/transaction" "/old-native-path/" "/new-native-path/"

# Native URL validation runs once while generated and native nginx locations
# intentionally coexist. Final validation must still reject that state.
source "$ROOT_DIR/scripts/lib/native_subscription.sh"
wm_native_capabilities_json() {
  cat <<'JSON'
{"clients_api":true,"settings_api":true,"inbounds_api":true,"native_listener_loopback":true,"sub_enable":true,"sub_listen":"127.0.0.1","sub_port":2096,"sub_path_matches":true,"custom_renderer_locations":true,"ready":false}
JSON
}
wm_native_require_capabilities "$tmp/native.json" true
if wm_native_require_capabilities "$tmp/native.json" false; then
  echo "strict native capabilities unexpectedly accepted generated renderer coexistence" >&2
  exit 1
fi

curl_attempts=0
curl() {
  local output="" previous="" argument
  curl_attempts=$((curl_attempts+1))
  for argument in "$@"; do
    if [[ "$previous" == "-o" ]]; then output="$argument"; break; fi
    previous="$argument"
  done
  (( curl_attempts >= 3 )) || return 22
  printf 'native subscription payload\n' > "$output"
}
sleep() { return 0; }
wm_native_fetch_public "https://entry.example.com/new-native-path/client-id" "$tmp/native.payload"
[[ "$curl_attempts" == "3" ]] || { echo "native readiness retry count differs from expectation" >&2; exit 1; }
grep -Fq 'native subscription payload' "$tmp/native.payload"

echo "baseline tests: OK"
