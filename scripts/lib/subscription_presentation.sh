#!/usr/bin/env bash

wm_find_xui_settings_db() {
  local path
  if [[ -n "${WM_XUI_DB:-}" && -f "$WM_XUI_DB" ]]; then
    printf '%s\n' "$WM_XUI_DB"
    return 0
  fi
  for path in /etc/x-ui/x-ui.db /usr/local/x-ui/bin/x-ui.db /usr/local/x-ui/x-ui.db; do
    [[ -f "$path" ]] && { printf '%s\n' "$path"; return 0; }
  done
  return 1
}

wm_apply_subscription_presentation() {
  local db changed
  db="$(wm_find_xui_settings_db)" || wm_fail "Could not locate 3X-UI database for subscription presentation"
  changed="$(python3 - "$db" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
changed = 0
try:
    for key, value in (("remarkModel", "-o"), ("subShowInfo", "false")):
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
            changed = 1
        elif str(row[0]).lower() != value.lower():
            conn.execute("UPDATE settings SET value=? WHERE key=?", (value, key))
            changed = 1
    conn.commit()
finally:
    conn.close()
print(changed)
PY
)" || return 1
  if [[ "$changed" == "1" ]]; then
    systemctl restart x-ui || return 1
    systemctl is-active --quiet x-ui || return 1
  fi
}
