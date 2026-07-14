#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if ! command -v python3 >/dev/null 2>&1; then python3() { python "$@"; }; export -f python3; fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

WM_STATE_DIR="$tmp/state"
WM_CONFIG_JSON="$WM_STATE_DIR/config.json"
WM_RUNTIME_JSON="$WM_STATE_DIR/runtime.json"
WM_SUB_DIR="$tmp/subscriptions"
WM_NGINX_MANAGED_CONF="$tmp/nginx.conf"
WM_TRANSACTION_ROOT="$WM_STATE_DIR/transactions"
NODE_ROLE="standalone"
mkdir -p "$WM_STATE_DIR" "$WM_SUB_DIR/users"
xui_db="$tmp/x-ui.db"
python3 - "$WM_CONFIG_JSON" "$xui_db" <<'PY'
import json,sqlite3,sys
json.dump({"schema_version":2,"installation":{"xui":{"database_path":sys.argv[2]}}},open(sys.argv[1],"w",encoding="utf-8"))
db=sqlite3.connect(sys.argv[2]); db.execute("create table state(value text)"); db.execute("insert into state values ('original')"); db.commit(); db.close()
PY
printf '{"node_status":"healthy"}\n' > "$WM_RUNTIME_JSON"
printf 'original nginx\n' > "$WM_NGINX_MANAGED_CONF"
printf 'original subscription\n' > "$WM_SUB_DIR/users/test.txt"

wm_warn() { :; }
wm_fail() { echo "$*" >&2; return 1; }
wm_load_config() { return 0; }
install() {
  if [[ "${1:-}" == "-m" ]]; then shift 2; fi
  cp "$1" "$2"
}
nginx() { return 0; }
systemctl() { return 0; }

source "$ROOT_DIR/scripts/lib/transaction.sh"

wm_transaction_begin "unit-rollback"
transaction="$WM_ACTIVE_TRANSACTION"
printf '{"schema_version":99}\n' > "$WM_CONFIG_JSON"
printf 'changed nginx\n' > "$WM_NGINX_MANAGED_CONF"
printf 'changed subscription\n' > "$WM_SUB_DIR/users/test.txt"
python3 - "$xui_db" <<'PY'
import sqlite3,sys
db=sqlite3.connect(sys.argv[1]); db.execute("update state set value='changed'"); db.commit(); db.close()
PY
wm_transaction_rollback "$transaction" "unit failure"

grep -q '"schema_version": 2' "$WM_CONFIG_JSON"
grep -q 'original nginx' "$WM_NGINX_MANAGED_CONF"
grep -q 'original subscription' "$WM_SUB_DIR/users/test.txt"
[[ "$(stat -c '%a' "$WM_SUB_DIR")" == "755" ]]
[[ "$(stat -c '%a' "$WM_SUB_DIR/users")" == "755" ]]
[[ "$(stat -c '%a' "$WM_SUB_DIR/users/test.txt")" == "644" ]]
python3 - "$xui_db" <<'PY'
import sqlite3,sys
assert sqlite3.connect(sys.argv[1]).execute("select value from state").fetchone()[0] == "original"
PY
python3 - "$transaction/result.json" <<'PY'
import json,sys
assert json.load(open(sys.argv[1],encoding="utf-8"))["status"] == "rolled_back"
PY

set +e
(
  wm_transaction_begin "unit-automatic-rollback"
  printf '{"schema_version":99}\n' > "$WM_CONFIG_JSON"
  exit 7
)
status=$?
set -e
[[ "$status" == "7" ]]
grep -q '"schema_version": 2' "$WM_CONFIG_JSON"

wm_transaction_begin "unit-commit"
transaction="$WM_ACTIVE_TRANSACTION"
wm_transaction_commit "$transaction"
python3 - "$transaction/result.json" <<'PY'
import json,sys
assert json.load(open(sys.argv[1],encoding="utf-8"))["status"] == "committed"
PY

trap 'rm -rf "$tmp"' EXIT
echo "transaction shell tests: OK"
