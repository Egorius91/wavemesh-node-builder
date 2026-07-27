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
chmod 0644 "$DESTDIR/etc/wavemesh-agent/agent.env"

run_installer() {
  PATH="$BIN_DIR:$PATH" \
  WAVEMESH_AGENT_DESTDIR="$DESTDIR" \
  WAVEMESH_AGENT_SYSTEMCTL="$BIN_DIR/systemctl" \
  WAVEMESH_AGENT_PYTHON="${WAVEMESH_TEST_PYTHON:-/usr/bin/python3}" \
  WAVEMESH_TEST_SYSTEMCTL_LOG="$SYSTEMCTL_LOG" \
    bash "$ROOT_DIR/agent/install.sh"
}

run_rollback() {
  PATH="$BIN_DIR:$PATH" \
  WAVEMESH_AGENT_DESTDIR="$DESTDIR" \
  WAVEMESH_AGENT_SYSTEMCTL="$BIN_DIR/systemctl" \
  WAVEMESH_AGENT_PYTHON="${WAVEMESH_TEST_PYTHON:-/usr/bin/python3}" \
  WAVEMESH_TEST_SYSTEMCTL_LOG="$SYSTEMCTL_LOG" \
    bash "$DESTDIR/usr/local/sbin/wavemesh-node-agent-rollback" "$@"
}

run_installer

for file in node_mtls_client.py node_mtls_runtime.py node_mtls_state.py; do
  [[ -f "$DESTDIR/usr/local/lib/wavemesh-agent/$file" ]]
  [[ "$(stat -c '%a' "$DESTDIR/usr/local/lib/wavemesh-agent/$file")" == 644 ]]
done
[[ "$(stat -c '%a' "$DESTDIR/usr/local/lib/wavemesh-agent/node_agent.py")" == 755 ]]
[[ "$(stat -c '%a' "$DESTDIR/usr/local/lib/wavemesh-agent/acceptance.py")" == 755 ]]
[[ "$(stat -c '%a' "$DESTDIR/etc/wavemesh-agent/agent.env")" == 600 ]]
[[ "$(stat -c '%a' "$DESTDIR/etc/wavemesh-agent/tls")" == 700 ]]
[[ "$(stat -c '%a' "$DESTDIR/etc/wavemesh-agent/tls/pending")" == 700 ]]
[[ "$(stat -c '%a' "$DESTDIR/etc/wavemesh-agent/tls/generations")" == 700 ]]
grep -Fx 'WAVEMESH_AGENT_MTLS_MODE=disabled' "$DESTDIR/etc/wavemesh-agent/agent.env" >/dev/null
[[ "$(grep -c '^WAVEMESH_AGENT_MTLS_MODE=' "$DESTDIR/etc/wavemesh-agent/agent.env")" == 1 ]]
if grep -Eq -- '-----BEGIN [A-Z0-9 ]+-----' "$DESTDIR/etc/wavemesh-agent/agent.env"; then
  echo "installer placed PEM material in agent.env" >&2
  exit 1
fi
grep -Fx 'daemon-reload' "$SYSTEMCTL_LOG" >/dev/null
grep -Fx 'enable wavemesh-node-agent.service' "$SYSTEMCTL_LOG" >/dev/null
if grep -Eq '(^| )(start|restart|try-restart|reload-or-restart)( |$)|--now' "$SYSTEMCTL_LOG"; then
  echo "installer started or restarted the Agent" >&2
  exit 1
fi

backup_root="$DESTDIR/var/lib/wavemesh-agent/backups"
first_backup_count="$(find "$backup_root" -mindepth 1 -maxdepth 1 -type d | wc -l)"
[[ "$first_backup_count" == 1 ]]

: > "$SYSTEMCTL_LOG"
run_installer
second_backup_count="$(find "$backup_root" -mindepth 1 -maxdepth 1 -type d | wc -l)"
[[ "$second_backup_count" == "$first_backup_count" ]]
if grep -Fx 'daemon-reload' "$SYSTEMCTL_LOG" >/dev/null; then
  echo "idempotent installer reloaded an unchanged unit" >&2
  exit 1
fi
if grep -Eq '(^| )(start|restart|try-restart|reload-or-restart)( |$)|--now' "$SYSTEMCTL_LOG"; then
  echo "idempotent installer started or restarted the Agent" >&2
  exit 1
fi

printf '\n# previous-version-marker\n' >> "$DESTDIR/usr/local/lib/wavemesh-agent/node_agent.py"
: > "$SYSTEMCTL_LOG"
run_installer
third_backup_count="$(find "$backup_root" -mindepth 1 -maxdepth 1 -type d | wc -l)"
[[ "$third_backup_count" -eq $((second_backup_count + 1)) ]]
if grep -Fq 'previous-version-marker' "$DESTDIR/usr/local/lib/wavemesh-agent/node_agent.py"; then
  echo "installer did not replace the previous Agent version" >&2
  exit 1
fi

: > "$SYSTEMCTL_LOG"
run_rollback --latest
grep -Fq 'previous-version-marker' "$DESTDIR/usr/local/lib/wavemesh-agent/node_agent.py"
if grep -Eq '(^| )restart( |$)' "$SYSTEMCTL_LOG"; then
  echo "rollback restarted the Agent without explicit request" >&2
  exit 1
fi

: > "$SYSTEMCTL_LOG"
run_rollback --latest --restart
grep -Fx 'restart wavemesh-node-agent.service' "$SYSTEMCTL_LOG" >/dev/null

printf '\n# unit-change-marker\n' >> "$DESTDIR/etc/systemd/system/wavemesh-node-agent.service"
: > "$SYSTEMCTL_LOG"
run_installer
grep -Fx 'daemon-reload' "$SYSTEMCTL_LOG" >/dev/null
if grep -Eq '(^| )(start|restart|try-restart|reload-or-restart)( |$)|--now' "$SYSTEMCTL_LOG"; then
  echo "unit update restarted the Agent" >&2
  exit 1
fi

unsafe_dest="$TEMP_DIR/unsafe-root"
mkdir -p "$unsafe_dest/etc/wavemesh-agent"
cat > "$unsafe_dest/etc/wavemesh-agent/agent.env" <<EOF
WAVEMESH_API_BASE=https://api.example.invalid/api
WAVEMESH_NODE_ID=node_12345678
WAVEMESH_TENANT_ID=tenant_12345678
WAVEMESH_AGENT_TOKEN=$token
WAVEMESH_AGENT_TOKEN_EXPIRES_AT=2030-01-01T00:00:00Z
EOF
printf '%s%s%s\n' '-----BEGIN ' 'CERTIFICATE' '-----' \
  >> "$unsafe_dest/etc/wavemesh-agent/agent.env"
if PATH="$BIN_DIR:$PATH" \
  WAVEMESH_AGENT_DESTDIR="$unsafe_dest" \
  WAVEMESH_AGENT_SYSTEMCTL="$BIN_DIR/systemctl" \
  WAVEMESH_AGENT_PYTHON="${WAVEMESH_TEST_PYTHON:-/usr/bin/python3}" \
  WAVEMESH_TEST_SYSTEMCTL_LOG="$SYSTEMCTL_LOG" \
    bash "$ROOT_DIR/agent/install.sh" >/dev/null 2>&1; then
  echo "installer accepted PEM material in agent.env" >&2
  exit 1
fi
[[ ! -e "$unsafe_dest/usr/local/lib/wavemesh-agent/node_agent.py" ]]

if command -v systemd-analyze >/dev/null 2>&1; then
  verify_dir="$TEMP_DIR/systemd-verify"
  mkdir -p "$verify_dir"
  sed \
    -e 's/^After=.*/After=network-online.target/' \
    -e 's#^ExecStart=.*#ExecStart=/usr/bin/python3 -c pass#' \
    "$ROOT_DIR/agent/wavemesh-node-agent.service" \
    > "$verify_dir/wavemesh-node-agent.service"
  SYSTEMD_UNIT_PATH="$verify_dir:/usr/lib/systemd/system:/lib/systemd/system" \
    systemd-analyze verify "$verify_dir/wavemesh-node-agent.service"
fi

echo "node agent installer smoke tests: OK"
