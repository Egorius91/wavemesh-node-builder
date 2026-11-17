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
json.dump({"schema_version":2,"node":{"role":"standalone"},"installation":{"xui":{"database_path":sys.argv[2]}}},open(sys.argv[1],"w",encoding="utf-8"))
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
# A lost prepared Xray response can still have a writer in flight. Even an
# apparently usable old snapshot cannot authorize a second mutation/restore.
wm_transaction_begin "unit-prepared-validation"
transaction="$WM_ACTIVE_TRANSACTION"
printf '{"status":"NO_CONFIG_MUTATION_DISPATCHED"}\n' > "$transaction/prepared.no-effect.json"
printf '{"schema_version":98}\n' > "$WM_CONFIG_JSON"
wm_transaction_rollback "$transaction" "unsupported engine before dispatch"
grep -q '"schema_version":98' "$WM_CONFIG_JSON"
python3 - "$transaction/result.json" <<'PY'
import json,sys
assert json.load(open(sys.argv[1],encoding='utf-8'))['status']=='rolled_back'
PY
wm_transaction_begin "unit-prepared-unknown"
transaction="$WM_ACTIVE_TRANSACTION"
printf '{"status":"UNKNOWN_UNTIL_HTTP_SUCCESS"}\n' > "$transaction/prepared.apply.pending.json"
printf '{"schema_version":99}\n' > "$WM_CONFIG_JSON"
if wm_transaction_rollback "$transaction" "lost prepared reply"; then exit 71; fi
grep -q '"schema_version":99' "$WM_CONFIG_JSON"
python3 - "$transaction/result.json" <<'PY'
import json,sys
assert json.load(open(sys.argv[1],encoding='utf-8'))['status']=='rollback_failed'
PY
[[ -f "$transaction/prepared.apply.pending.json" ]]
if python3 "$WM_TRANSACTION_TOOL" check --root "$WM_TRANSACTION_ROOT"; then exit 72; fi
python3 "$WM_TRANSACTION_TOOL" prune --root "$WM_TRANSACTION_ROOT" --keep 0
[[ -f "$transaction/prepared.apply.pending.json" ]]
trap 'rm -rf "$tmp"' EXIT

# Pending presence is authoritative even when JSON is malformed or the marker
# is a dangling symlink. Stop before recovery checks or any restore/restart.
for pending_kind in malformed dangling; do
  transaction="$tmp/prepared-${pending_kind}"
  mkdir -p "$transaction"
  printf '{}\n' > "$transaction/plan.json"
  if [[ "$pending_kind" == dangling ]]; then
    ln -s "$transaction/absent-target.json" "$transaction/prepared.apply.pending.json"
    [[ -L "$transaction/prepared.apply.pending.json" && ! -e "$transaction/prepared.apply.pending.json" ]]
  else
    printf '{broken-json\n' > "$transaction/prepared.apply.pending.json"
  fi
  (
    effect="$transaction/unsafe-effect"
    wm_transaction_assert_xray_recovery() { printf 'recovery\n' >> "$effect"; return 0; }
    wm_atomic_install_json() { printf 'restore\n' >> "$effect"; }
    wm_load_config() { printf 'load\n' >> "$effect"; }
    wm_xray_apply_template() { printf 'POST\n' >> "$effect"; }
    systemctl() { printf 'restart\n' >> "$effect"; }
    nginx() { printf 'nginx\n' >> "$effect"; }
    if wm_transaction_rollback "$transaction" "pending ${pending_kind}"; then exit 73; fi
    [[ ! -e "$effect" ]]
  )
  python3 - "$transaction" <<'PY'
import json, os, sys
from pathlib import Path
transaction = Path(sys.argv[1])
assert os.path.lexists(transaction / "prepared.apply.pending.json")
assert json.loads((transaction / "result.json").read_text(encoding="utf-8"))["status"] == "rollback_failed"
assert not (transaction / "unsafe-effect").exists()
PY
done
# A known-result origin also blocks restore when completed is malformed or
# dangling. Generic rollback_failed must not erase the retained proof.
for completed_kind in malformed dangling; do
  transaction="$tmp/completed-${completed_kind}"
  mkdir -p "$transaction"
  printf '{"schema_version":1,"operation":"auto-prepared-reconcile"}\n' > "$transaction/plan.json"
  printf '{"status":"committed","prepared_commit":{"malformed":"retain"}}\n' > "$transaction/result.json"
  cp "$transaction/result.json" "$transaction/result.before"
  if [[ "$completed_kind" == dangling ]]; then
    ln -s "$transaction/absent-target.json" "$transaction/prepared.apply.completed.json"
    [[ -L "$transaction/prepared.apply.completed.json" && ! -e "$transaction/prepared.apply.completed.json" ]]
  else
    printf '{broken-json\n' > "$transaction/prepared.apply.completed.json"
  fi
  (
    effect="$transaction/unsafe-effect"
    wm_transaction_assert_xray_recovery() { printf 'recovery\n' >> "$effect"; return 0; }
    wm_atomic_install_json() { printf 'restore\n' >> "$effect"; }
    wm_load_config() { printf 'load\n' >> "$effect"; }
    wm_xray_apply_template() { printf 'POST\n' >> "$effect"; }
    systemctl() { printf 'restart\n' >> "$effect"; }
    nginx() { printf 'nginx\n' >> "$effect"; }
    if wm_transaction_rollback "$transaction" "completed ${completed_kind}"; then exit 74; fi
    [[ ! -e "$effect" ]]
  )
  cmp "$transaction/result.before" "$transaction/result.json"
  python3 - "$transaction" <<'PYCASE'
import os, sys
from pathlib import Path
transaction = Path(sys.argv[1])
assert os.path.lexists(transaction / "prepared.apply.completed.json")
assert not (transaction / "unsafe-effect").exists()
PYCASE
 done
echo "transaction shell tests: OK"
