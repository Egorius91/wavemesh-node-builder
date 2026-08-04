#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE="wavemesh-node-agent.service"
PYTHON="${WAVEMESH_AGENT_PYTHON:-/usr/bin/python3}"
INSTALL_DIR="${WAVEMESH_AGENT_INSTALL_DIR:-/usr/local/lib/wavemesh-agent}"
ENV_FILE="${WAVEMESH_AGENT_ENV:-/etc/wavemesh-agent/agent.env}"
TOKEN_FILE="${WAVEMESH_RECOVERY_TOKEN_FILE:-/etc/wavemesh-agent/recovery.token}"
EXTERNAL_NODE_ID="${WAVEMESH_RECOVERY_EXTERNAL_NODE_ID:-}"
BACKUP_ROOT="${WAVEMESH_RECOVERY_BACKUP_ROOT:-/var/lib/wavemesh-agent/recovery-backups}"
LOCK_FILE="${WAVEMESH_RECOVERY_LOCK_FILE:-/run/lock/wavemesh-node-recovery.lock}"
RECOVERY_CLIENT="$INSTALL_DIR/node_recovery.py"
AGENT="$INSTALL_DIR/node_agent.py"
TLS_RUNTIME="/etc/wavemesh-agent/tls/runtime.json"

fail() {
  printf 'NODE_RECOVERY_ERROR=%s\n' "$1" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || fail root_required
[[ -n "$EXTERNAL_NODE_ID" ]] || fail external_node_id_required
[[ -x "$PYTHON" ]] || fail python_missing
[[ -f "$RECOVERY_CLIENT" && ! -L "$RECOVERY_CLIENT" ]] || fail recovery_client_missing
[[ -f "$AGENT" && ! -L "$AGENT" ]] || fail agent_missing
[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail agent_environment_missing
[[ -f "$TOKEN_FILE" && ! -L "$TOKEN_FILE" ]] || fail recovery_token_missing
[[ "$(stat -c '%a' "$ENV_FILE")" == "600" ]] || fail agent_environment_permissions
[[ "$(stat -c '%a' "$TOKEN_FILE")" == "600" ]] || fail recovery_token_permissions

install -d -o root -g root -m 0700 "$(dirname "$LOCK_FILE")" "$BACKUP_ROOT"
exec 9>"$LOCK_FILE"
flock -n 9 || fail recovery_already_running

backup_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
backup_dir="$BACKUP_ROOT/$backup_id"
install -d -o root -g root -m 0700 "$backup_dir"

printf 'node_recovery_started_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'node_recovery_external_node_id=%s\n' "$EXTERNAL_NODE_ID"
printf 'node_recovery_backup_dir=%s\n' "$backup_dir"

service_was_active=no
if systemctl is-active --quiet "$SERVICE"; then
  service_was_active=yes
fi
printf 'node_recovery_service_was_active=%s\n' "$service_was_active"

install -o root -g root -m 0600 "$ENV_FILE" "$backup_dir/agent.env.before"
install -o root -g root -m 0600 "$TOKEN_FILE" "$backup_dir/recovery.token.before"
if [[ -d /etc/wavemesh-agent/tls && ! -L /etc/wavemesh-agent/tls ]]; then
  tar --numeric-owner --xattrs --acls -C /etc/wavemesh-agent -cpf "$backup_dir/tls.before.tar" tls
  chmod 0600 "$backup_dir/tls.before.tar"
fi
for state_file in \
  /etc/wavemesh-agent/rotation.pending \
  /etc/wavemesh-agent/recovery.pending \
  /etc/wavemesh-agent/recovery.accepted.json; do
  if [[ -f "$state_file" && ! -L "$state_file" ]]; then
    install -o root -g root -m 0600 "$state_file" "$backup_dir/${state_file##*/}.before"
  fi
done
systemctl show "$SERVICE" --property=ActiveState,SubState,MainPID,NRestarts > "$backup_dir/service.before.txt"
chmod 0600 "$backup_dir/service.before.txt"
(
  cd "$backup_dir"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
)
chmod 0600 "$backup_dir/SHA256SUMS"
printf 'node_recovery_private_backup=PASS\n'

"$PYTHON" "$RECOVERY_CLIENT" check \
  --env-file "$ENV_FILE" \
  --token-file "$TOKEN_FILE" \
  --external-node-id "$EXTERNAL_NODE_ID" \
  > "$backup_dir/preflight.json"
chmod 0600 "$backup_dir/preflight.json"
printf 'node_recovery_client_preflight=PASS\n'

systemctl stop "$SERVICE"
if systemctl is-active --quiet "$SERVICE"; then
  fail agent_service_did_not_stop
fi
printf 'node_recovery_agent_stopped=yes\n'

set +e
"$PYTHON" "$RECOVERY_CLIENT" apply \
  --env-file "$ENV_FILE" \
  --token-file "$TOKEN_FILE" \
  --external-node-id "$EXTERNAL_NODE_ID" \
  > "$backup_dir/recovery-result.json" \
  2> "$backup_dir/recovery-error.txt"
recovery_rc=$?
set -e
chmod 0600 "$backup_dir/recovery-result.json" "$backup_dir/recovery-error.txt"

if [[ "$recovery_rc" -ne 0 ]]; then
  printf 'node_recovery_remote_or_local_apply=FAILED\n' >&2
  printf 'node_recovery_retry_safe=yes\n' >&2
  printf 'node_recovery_agent_left_stopped=yes\n' >&2
  printf 'node_recovery_diagnostic=%s\n' "$backup_dir/recovery-error.txt" >&2
  exit "$recovery_rc"
fi
printf 'node_recovery_remote_or_local_apply=PASS\n'

"$PYTHON" "$AGENT" check --env-file "$ENV_FILE" > "$backup_dir/agent-check.after.json"
chmod 0600 "$backup_dir/agent-check.after.json"
printf 'node_recovery_agent_config_after=PASS\n'

systemctl start "$SERVICE"
service_ready=no
mtls_ready=no
for _ in $(seq 1 90); do
  if systemctl is-active --quiet "$SERVICE"; then
    service_ready=yes
  fi
  if [[ -f "$TLS_RUNTIME" && ! -L "$TLS_RUNTIME" ]]; then
    state="$($PYTHON - "$TLS_RUNTIME" <<'PY'
import json
import sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("INVALID")
else:
    print(value.get("state", "INVALID"))
PY
)"
    if [[ "$state" == "SHADOW_ACTIVE" ]]; then
      mtls_ready=yes
      break
    fi
  fi
  sleep 2
done

[[ "$service_ready" == yes ]] || fail agent_service_not_active_after_recovery
[[ "$mtls_ready" == yes ]] || fail mtls_identity_not_active_after_recovery

systemctl show "$SERVICE" --property=ActiveState,SubState,MainPID,NRestarts > "$backup_dir/service.after.txt"
chmod 0600 "$backup_dir/service.after.txt"

[[ ! -e "$TOKEN_FILE" && ! -L "$TOKEN_FILE" ]] || fail recovery_token_not_destroyed
[[ ! -e /etc/wavemesh-agent/recovery.pending && ! -L /etc/wavemesh-agent/recovery.pending ]] || fail recovery_pending_not_destroyed
[[ ! -e /etc/wavemesh-agent/recovery.accepted.json && ! -L /etc/wavemesh-agent/recovery.accepted.json ]] || fail recovery_marker_not_destroyed

[[ -f "$backup_dir/recovery.token.before" && ! -L "$backup_dir/recovery.token.before" ]] || fail recovery_backup_token_missing
rm -f -- "$backup_dir/recovery.token.before"
[[ ! -e "$backup_dir/recovery.token.before" && ! -L "$backup_dir/recovery.token.before" ]] || fail recovery_backup_token_not_destroyed
(
  cd "$backup_dir"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS.next
  chmod 0600 SHA256SUMS.next
  mv -fT SHA256SUMS.next SHA256SUMS
)
printf 'node_recovery_backup_token_destroyed=yes\n'

printf 'node_recovery_service_active=yes\n'
printf 'node_recovery_mtls_state=SHADOW_ACTIVE\n'
printf 'node_recovery_one_time_token_destroyed=yes\n'
printf 'node_recovery_pending_secret_destroyed=yes\n'
printf 'node_recovery_old_generations_retained=yes\n'
printf 'NODE_SCOPED_CREDENTIAL_RECOVERY=PASS\n'
