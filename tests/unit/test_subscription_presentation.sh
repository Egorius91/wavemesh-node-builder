#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

export WM_XUI_DB="$TEMP_DIR/x-ui.db"
python3 - "$WM_XUI_DB" <<'PY'
import sqlite3
import sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT UNIQUE, value TEXT)")
conn.execute("INSERT INTO settings(key,value) VALUES('remarkModel','-ieo')")
conn.execute("INSERT INTO settings(key,value) VALUES('subShowInfo','true')")
conn.commit()
conn.close()
PY

mkdir -p "$TEMP_DIR/bin"
cat > "$TEMP_DIR/bin/systemctl" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == "restart" ]]; then printf 'restart\n' >> "$WM_SYSTEMCTL_LOG"; fi
exit 0
SH
chmod +x "$TEMP_DIR/bin/systemctl"
export PATH="$TEMP_DIR/bin:$PATH"
export WM_SYSTEMCTL_LOG="$TEMP_DIR/systemctl.log"

wm_fail() { echo "$*" >&2; return 1; }
source "$ROOT_DIR/scripts/lib/subscription_presentation.sh"
wm_apply_subscription_presentation

python3 - "$WM_XUI_DB" <<'PY'
import sqlite3
import sys
conn = sqlite3.connect(sys.argv[1])
values = dict(conn.execute("SELECT key,value FROM settings"))
assert values["remarkModel"] == "-o"
assert values["subShowInfo"] == "false"
conn.close()
PY

[[ "$(wc -l < "$WM_SYSTEMCTL_LOG")" -eq 1 ]]
wm_apply_subscription_presentation
[[ "$(wc -l < "$WM_SYSTEMCTL_LOG")" -eq 1 ]]

echo "subscription presentation tests: OK"
