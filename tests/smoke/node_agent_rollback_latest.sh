#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

BIN_DIR="$TEMP_DIR/bin"
DESTDIR="$TEMP_DIR/root"
SYSTEMCTL_LOG="$TEMP_DIR/systemctl.log"
mkdir -p "$BIN_DIR" "$DESTDIR/etc/wavemesh-agent"

cat > "$BIN_DIR/wavemesh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat > "$BIN_DIR/systemctl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$WAVEMESH_TEST_SYSTEMCTL_LOG"
exit 0
SH
chmod +x "$BIN_DIR/wavemesh" "$BIN_DIR/systemctl"

token="wvn_$(printf 'a%.0s' {1..40})"
cat > "$DESTDIR/etc/wavemesh-agent/agent.env" <<EOF
WAVEMESH_API_BASE=https://api.example.invalid/api
WAVEMESH_NODE_ID=node_12345678
WAVEMESH_TENANT_ID=tenant_12345678
WAVEMESH_AGENT_TOKEN=$token
WAVEMESH_AGENT_TOKEN_EXPIRES_AT=2030-01-01T00:00:00Z
EOF
chmod 0600 "$DESTDIR/etc/wavemesh-agent/agent.env"

run_installer() {
  PATH="$BIN_DIR:$PATH" \
  WAVEMESH_AGENT_DESTDIR="$DESTDIR" \
  WAVEMESH_AGENT_SYSTEMCTL="$BIN_DIR/systemctl" \
  WAVEMESH_AGENT_PYTHON="${WAVEMESH_TEST_PYTHON:-/usr/bin/python3}" \
  WAVEMESH_TEST_SYSTEMCTL_LOG="$SYSTEMCTL_LOG" \
    bash "$ROOT_DIR/agent/install.sh" >/dev/null
}

run_rollback() {
  mkdir -p "$DESTDIR/run/lock"
  touch "$DESTDIR/run/lock/wavemesh-node.lock"
  PATH="$BIN_DIR:$PATH" \
  WAVEMESH_AGENT_DESTDIR="$DESTDIR" \
  WAVEMESH_AGENT_SYSTEMCTL="$BIN_DIR/systemctl" \
  WAVEMESH_AGENT_PYTHON="${WAVEMESH_TEST_PYTHON:-/usr/bin/python3}" \
  WAVEMESH_TEST_SYSTEMCTL_LOG="$SYSTEMCTL_LOG" \
    bash "$DESTDIR/usr/local/sbin/wavemesh-node-agent-rollback" "$@"
}

run_installer
printf 'preserved private evidence\n' > "$DESTDIR/var/lib/wavemesh-agent/runtime-findings/evidence-marker"
chmod 0600 "$DESTDIR/var/lib/wavemesh-agent/runtime-findings/evidence-marker"
printf '\n# rollback-latest-marker\n' >> "$DESTDIR/usr/local/lib/wavemesh-agent/node_agent.py"
run_installer

backup_root="$DESTDIR/var/lib/wavemesh-agent/backups"
mkdir -p "$backup_root/stale-upstream-release-20990101T000000Z"

run_rollback --latest >/dev/null
grep -Fq 'rollback-latest-marker' "$DESTDIR/usr/local/lib/wavemesh-agent/node_agent.py"
grep -Fx 'preserved private evidence' "$DESTDIR/var/lib/wavemesh-agent/runtime-findings/evidence-marker" >/dev/null

# A local maintenance hold blocks actual installed rollback before source or
# service effects. After cancellation, only compatible backups can be restored.
journal="$DESTDIR/var/lib/wavemesh-agent/panel-requests"
maintenance_fixture() {
  PYTHONPATH="$ROOT_DIR/agent" WAVEMESH_PANEL_REQUEST_STATE_DIR="$journal" \
    python3 - "$1" <<'PY'
import sys
from panel_request_guard import PanelRequestGuard
guard=PanelRequestGuard()
with guard.locked():
    guard.maintenance(sys.argv[1], '00000000-0000-4000-8000-000000000001', 1)
PY
}
maintenance_fixture prepare
cp "$DESTDIR/usr/local/lib/wavemesh-agent/node_agent.py" "$TEMP_DIR/agent.before-hold"
: > "$SYSTEMCTL_LOG"
if run_rollback --latest --restart >/dev/null 2>&1; then
  echo "rollback ignored maintenance hold" >&2; exit 1
fi
cmp "$TEMP_DIR/agent.before-hold" "$DESTDIR/usr/local/lib/wavemesh-agent/node_agent.py"
[[ ! -s "$SYSTEMCTL_LOG" ]]
maintenance_fixture cancel
cp "$journal/state.json" "$TEMP_DIR/history.before-rollback"
latest_backup="$(find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name '2*T*-*' | sort | tail -n 1)"
cp "$latest_backup/panel_request_guard.py" "$TEMP_DIR/compatible-guard"
sed -i '/^MAINTENANCE_PROTOCOL = /d' "$latest_backup/panel_request_guard.py"
if run_rollback --latest --restart >/dev/null 2>&1; then
  echo "rollback accepted a target without maintenance protocol support" >&2; exit 1
fi
cmp "$TEMP_DIR/agent.before-hold" "$DESTDIR/usr/local/lib/wavemesh-agent/node_agent.py"
[[ ! -s "$SYSTEMCTL_LOG" ]]
cp "$TEMP_DIR/compatible-guard" "$latest_backup/panel_request_guard.py"
run_rollback --latest >/dev/null
cmp "$TEMP_DIR/history.before-rollback" "$journal/state.json"
[[ ! -s "$SYSTEMCTL_LOG" ]]

# A v3 installation intent is never reopened by ordinary source rollback.
PYTHONPATH="$ROOT_DIR/agent" WAVEMESH_PANEL_REQUEST_STATE_DIR="$journal" \
  python3 - "$TEMP_DIR/install-intent.lock" <<'PY'
import sys
from pathlib import Path
from panel_request_guard import PanelRequestGuard, maintenance_node_lock
guard = PanelRequestGuard()
operation = '00000000-0000-4000-8000-000000000001'
lock = Path(sys.argv[1])
with maintenance_node_lock(lock), guard.locked():
    guard.maintenance('prepare', operation, 2)
with guard.installation_intent(operation, 2, 'a' * 64, 'b' * 64, lock):
    pass
PY
cp "$journal/state.json" "$TEMP_DIR/install-intent.before-rollback"
if run_rollback --latest --restart >/dev/null 2>&1; then
  echo "rollback ignored non-cancellable installation intent" >&2; exit 1
fi
cmp "$TEMP_DIR/agent.before-hold" "$DESTDIR/usr/local/lib/wavemesh-agent/node_agent.py"
cmp "$TEMP_DIR/install-intent.before-rollback" "$journal/state.json"
[[ ! -s "$SYSTEMCTL_LOG" ]]

mkdir -p "$backup_root/20991231T235959Z-99999"
if run_rollback --latest >/dev/null 2>&1; then
  echo "rollback accepted canonical backup without a manifest" >&2
  exit 1
fi

echo "node agent latest rollback selection tests: OK"
