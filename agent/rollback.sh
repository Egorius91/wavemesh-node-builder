#!/usr/bin/env bash
set -Eeuo pipefail

DESTDIR="${WAVEMESH_AGENT_DESTDIR:-}"
SYSTEMCTL="${WAVEMESH_AGENT_SYSTEMCTL:-systemctl}"
PYTHON="${WAVEMESH_AGENT_PYTHON:-/usr/bin/python3}"
SERVICE="wavemesh-node-agent.service"
if [[ -z "$DESTDIR" ]]; then
  SYSTEMCTL=systemctl
  PYTHON=/usr/bin/python3
fi
RESTART=false
BACKUP_ID=""

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

ok() {
  echo "OK: $*"
}

usage() {
  echo "Usage: wavemesh-node-agent-rollback [--latest | --backup ID] [--restart]"
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --latest)
      [[ -z "$BACKUP_ID" ]] || fail "Select exactly one backup"
      BACKUP_ID=latest
      shift
      ;;
    --backup)
      [[ -z "$BACKUP_ID" && "$#" -ge 2 ]] || fail "Missing or duplicate backup ID"
      BACKUP_ID="$2"
      shift 2
      ;;
    --restart)
      RESTART=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "Unknown rollback argument"
      ;;
  esac
done

[[ -n "$BACKUP_ID" ]] || fail "Select --latest or --backup ID"
[[ -z "$DESTDIR" || "$DESTDIR" == /* ]] || fail "WAVEMESH_AGENT_DESTDIR must be absolute"
[[ "$DESTDIR" != "/" ]] || fail "WAVEMESH_AGENT_DESTDIR must not be /"
[[ -z "$DESTDIR" || ! -L "$DESTDIR" ]] || fail "WAVEMESH_AGENT_DESTDIR must not be a symlink"
if [[ -z "$DESTDIR" ]]; then
  [[ "${EUID}" -eq 0 ]] || fail "Run rollback as root"
fi
command -v "$PYTHON" >/dev/null 2>&1 || fail "python3 is required"
command -v "$SYSTEMCTL" >/dev/null 2>&1 || fail "systemctl is required"

ETC_DIR="$DESTDIR/etc/wavemesh-agent"
INSTALL_DIR="$DESTDIR/usr/local/lib/wavemesh-agent"
UNIT_PATH="$DESTDIR/etc/systemd/system/$SERVICE"
ROLLBACK_PATH="$DESTDIR/usr/local/sbin/wavemesh-node-agent-rollback"
BACKUP_ROOT="$DESTDIR/var/lib/wavemesh-agent/backups"
ENV_FILE="${WAVEMESH_AGENT_ENV:-$ETC_DIR/agent.env}"

if [[ "$BACKUP_ID" == latest ]]; then
  shopt -s nullglob
  backups=()
  for candidate in "$BACKUP_ROOT"/*; do
    backup_name="${candidate##*/}"
    [[ -d "$candidate" && ! -L "$candidate" ]] || continue
    [[ "$backup_name" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]] || continue
    backups+=("$candidate")
  done
  shopt -u nullglob
  [[ "${#backups[@]}" -gt 0 ]] || fail "No canonical Agent backups are available"
  backup_dir="${backups[${#backups[@]}-1]}"
else
  [[ "$BACKUP_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]] || fail "Backup ID is invalid"
  backup_dir="$BACKUP_ROOT/$BACKUP_ID"
fi
[[ -d "$backup_dir" && ! -L "$backup_dir" ]] || fail "Selected Agent backup is missing or unsafe"
[[ -f "$backup_dir/manifest" && ! -L "$backup_dir/manifest" ]] || fail "Selected Agent backup has no safe manifest"

install_directory() {
  local mode="$1" path="$2"
  [[ ! -L "$path" ]] || fail "Refusing to use a symlink directory"
  if [[ -z "$DESTDIR" ]]; then
    install -o root -g root -d -m "$mode" "$path"
  else
    install -d -m "$mode" "$path"
  fi
}

atomic_restore() {
  local source="$1" target="$2" mode="$3" parent_mode="$4" temporary
  [[ -f "$source" && ! -L "$source" ]] || fail "Backup file is missing or unsafe"
  install_directory "$parent_mode" "$(dirname "$target")"
  [[ ! -e "$target" || -f "$target" ]] || fail "Refusing to replace a non-file rollback target"
  temporary="$(mktemp "$(dirname "$target")/.${target##*/}.rollback.XXXXXX")"
  if [[ -z "$DESTDIR" ]]; then
    install -o root -g root -m "$mode" "$source" "$temporary"
  else
    install -m "$mode" "$source" "$temporary"
  fi
  mv -fT "$temporary" "$target"
}

restore_or_remove() {
  local label="$1" target="$2" mode="$3" parent_mode="$4"
  if [[ -f "$backup_dir/$label" && ! -L "$backup_dir/$label" ]]; then
    atomic_restore "$backup_dir/$label" "$target" "$mode" "$parent_mode"
  elif [[ -f "$backup_dir/$label.absent" && ! -L "$backup_dir/$label.absent" ]]; then
    [[ ! -L "$target" ]] || fail "Refusing to remove a symlink rollback target"
    rm -f -- "$target"
  else
    fail "Backup entry is incomplete"
  fi
}

# Exclude cooperating Agent/CLI writers for the entire source rollback. Never
# replace/truncate the shared lock inode. The installer creates it via tmpfiles.
node_lock="$DESTDIR/run/lock/wavemesh-node.lock"
[[ -f "$node_lock" && ! -L "$node_lock" && -O "$node_lock" ]] || fail "Node mutation lock is unavailable or unsafe"
[[ "$(stat -c '%h' "$node_lock")" == 1 ]] || fail "Node mutation lock is unsafe"
exec 9<>"$node_lock"
flock -n 9 || fail "Node mutation is busy"
[[ "$(stat -Lc '%d:%i' "/proc/$$/fd/9")" == "$(stat -c '%d:%i' "$node_lock")" ]] || fail "Node mutation lock changed"

panel_journal="$DESTDIR/var/lib/wavemesh-agent/panel-requests"
[[ -z "${WAVEMESH_PANEL_REQUEST_STATE_DIR:-}" || "$WAVEMESH_PANEL_REQUEST_STATE_DIR" == "$panel_journal" ]] || fail "Rollback requires the canonical panel request journal"
[[ ! -L "$panel_journal" ]] || fail "Panel request journal is unsafe"
if [[ ! -e "$panel_journal" ]]; then
  install_directory 0700 "$panel_journal"
fi
[[ -d "$panel_journal" && -O "$panel_journal" && "$(stat -c '%a' "$panel_journal")" == 700 ]] || fail "Panel request journal is unsafe"
[[ ! -L "$panel_journal/.lock" ]] || fail "Panel request lock is unsafe"
if [[ -e "$panel_journal/.lock" ]]; then
  [[ -f "$panel_journal/.lock" && -O "$panel_journal/.lock" && "$(stat -c '%h:%a' "$panel_journal/.lock")" == 1:600 ]] || fail "Panel request lock is unsafe"
fi
rollback_umask="$(umask)"
umask 077
exec 10<>"$panel_journal/.lock"
umask "$rollback_umask"
flock -n 10 || fail "Panel request is busy"
if [[ -e "$panel_journal/state.json" || -L "$panel_journal" || -L "$panel_journal/state.json" ]]; then
  [[ -f "$INSTALL_DIR/panel_request_guard.py" && ! -L "$INSTALL_DIR/panel_request_guard.py" ]] || fail "Panel request guard is unavailable"
  WAVEMESH_PANEL_REQUEST_STATE_DIR="$panel_journal" "$PYTHON" "$INSTALL_DIR/panel_request_guard.py" --check-open-held-lock >/dev/null 2>&1 || fail "Panel request reconciliation is required before rollback"
  [[ -f "$backup_dir/panel_request_guard.py" && ! -L "$backup_dir/panel_request_guard.py" ]] || fail "Rollback target predates panel request protection"
  if ! WAVEMESH_PANEL_REQUEST_STATE_DIR="$panel_journal" "$PYTHON" "$INSTALL_DIR/panel_request_guard.py" --check-v1-held-lock >/dev/null 2>&1; then
    grep -Fq 'MAINTENANCE_PROTOCOL = "local-maintenance-v2"' "$backup_dir/panel_request_guard.py" || fail "Rollback target cannot read maintenance history"
  fi
  grep -q 'panel_request_guard' "$backup_dir/access_runtime.py" || fail "Rollback target lacks panel request protection"
fi

unit_changed=true
if [[ -f "$backup_dir/$SERVICE" && -f "$UNIT_PATH" ]] && cmp -s "$backup_dir/$SERVICE" "$UNIT_PATH"; then
  unit_changed=false
elif [[ -f "$backup_dir/$SERVICE.absent" && ! -e "$UNIT_PATH" ]]; then
  unit_changed=false
fi

restore_or_remove node_agent.py "$INSTALL_DIR/node_agent.py" 0755 0755
restore_or_remove node_mtls_client.py "$INSTALL_DIR/node_mtls_client.py" 0644 0755
restore_or_remove node_mtls_runtime.py "$INSTALL_DIR/node_mtls_runtime.py" 0644 0755
restore_or_remove node_mtls_state.py "$INSTALL_DIR/node_mtls_state.py" 0644 0755
restore_or_remove acceptance.py "$INSTALL_DIR/acceptance.py" 0755 0755
restore_or_remove access_runtime.py "$INSTALL_DIR/access_runtime.py" 0755 0755
# Historical backups predate this optional collector. Private evidence is never
# rewound/deleted with code: old Agent versions simply leave it untouched.
if [[ -e "$backup_dir/runtime_findings.py" || -e "$backup_dir/runtime_findings.py.absent" ]]; then
  restore_or_remove runtime_findings.py "$INSTALL_DIR/runtime_findings.py" 0644 0755
fi
if [[ -e "$backup_dir/panel_request_guard.py" || -e "$backup_dir/panel_request_guard.py.absent" ]]; then
  restore_or_remove panel_request_guard.py "$INSTALL_DIR/panel_request_guard.py" 0644 0755
fi
restore_or_remove "$SERVICE" "$UNIT_PATH" 0644 0755
# Backward-compatible with backups made before shared-lock support. Leave the
# runtime inode alone, even when removing this boot-time creation rule.
if [[ -e "$backup_dir/wavemesh-node-lock.conf" || -e "$backup_dir/wavemesh-node-lock.conf.absent" ]]; then
  restore_or_remove wavemesh-node-lock.conf "$DESTDIR/etc/tmpfiles.d/wavemesh-node-lock.conf" 0644 0755
fi
restore_or_remove wavemesh-node-agent-rollback "$ROLLBACK_PATH" 0755 0755
restore_or_remove agent.env "$ENV_FILE" 0600 0700

if [[ -f "$ENV_FILE" ]] && grep -Eq -- '-----BEGIN [A-Z0-9 ]+-----' "$ENV_FILE"; then
  fail "Restored Agent environment contains PEM material"
fi
if [[ -f "$INSTALL_DIR/node_agent.py" && -f "$ENV_FILE" ]]; then
  "$PYTHON" "$INSTALL_DIR/node_agent.py" check --env-file "$ENV_FILE" >/dev/null
fi
if [[ "$unit_changed" == true ]]; then
  "$SYSTEMCTL" daemon-reload
fi
if [[ "$RESTART" == true ]]; then
  "$SYSTEMCTL" restart "$SERVICE"
fi

ok "Selected Agent backup restored atomically"
if [[ "$RESTART" == true ]]; then
  ok "Only $SERVICE was restarted"
else
  ok "Service was not restarted; activation remains an explicit operator step"
fi
