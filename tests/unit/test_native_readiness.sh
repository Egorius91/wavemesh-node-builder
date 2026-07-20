#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

wm_warn() { printf '%s\n' "$*" >&2; }
WM_NATIVE_READINESS_ATTEMPTS=3
WM_NATIVE_READINESS_DELAY_SECONDS=0
DOMAIN=entry.example.test
PUBLIC_IP=203.0.113.7
PANEL_PORT=50000
XHTTP_LOCAL_PORT=21000

cat > "$tmp_dir/native-tool.py" <<'PY'
#!/usr/bin/env python3
import pathlib
import sys

command = sys.argv[1]
if command == "expected-profiles":
    print(1)
elif command == "validate-content":
    args = dict(zip(sys.argv[2::2], sys.argv[3::2]))
    content = pathlib.Path(args["--content"]).read_text(encoding="utf-8")
    expected = int(args["--expected-profiles"])
    profiles = sum(1 for line in content.splitlines() if line.startswith("vless://"))
    raise SystemExit(0 if profiles == expected else 1)
else:
    raise SystemExit(f"unexpected command: {command}")
PY
chmod +x "$tmp_dir/native-tool.py"
# shellcheck source=scripts/lib/native_subscription.sh
source "$ROOT_DIR/scripts/lib/native_subscription.sh"
WM_NATIVE_SUBSCRIPTION_TOOL="$tmp_dir/native-tool.py"

printf '{"network":{"subscription":{"path":"/opaque/"}}}\n' > "$tmp_dir/config.json"

profile_counter="$tmp_dir/profile-counter"
printf '0\n' > "$profile_counter"
wm_xui_request_success() {
  local count
  count=$(( $(cat "$profile_counter") + 1 ))
  printf '%s\n' "$count" > "$profile_counter"
  if (( count < 3 )); then
    printf '{"obj":["stale-a","stale-b","stale-c"]}\n'
  else
    printf '{"obj":["ready"]}\n'
  fi
}

links="$tmp_dir/links.json"
actual="$(wm_native_wait_profile_count "$tmp_dir/config.json" opaque-sub-id "$links")"
[[ "$actual" == "1" ]]
[[ "$(cat "$profile_counter")" == "3" ]]

public_counter="$tmp_dir/public-counter"
printf '0\n' > "$public_counter"
curl() {
  local count output=""
  count=$(( $(cat "$public_counter") + 1 ))
  printf '%s\n' "$count" > "$public_counter"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o) output="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  [[ -n "$output" ]]
  if (( count < 2 )); then
    printf 'vless://stale-one\nvless://stale-two\n' > "$output"
  else
    printf 'vless://ready\n' > "$output"
  fi
}

content="$tmp_dir/content.txt"
wm_native_wait_public_content "$tmp_dir/config.json" opaque-sub-id "$content" 1 "127.0.0.1"
[[ "$(cat "$public_counter")" == "2" ]]

WM_NATIVE_READINESS_ATTEMPTS=2
printf '0\n' > "$profile_counter"
if wm_native_wait_profile_count "$tmp_dir/config.json" opaque-sub-id "$links" >/dev/null 2>&1; then
  echo "profile readiness accepted a permanently stale response" >&2
  exit 1
fi

echo "native readiness tests: OK"
