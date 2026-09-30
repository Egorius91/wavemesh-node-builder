"""Exercise Exit writer/recovery boundaries with native-shaped local backups."""
import copy
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
bash = shutil.which("bash") or next(
    (p for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe") if Path(p).is_file()), None
)
assert bash, "bash is required"
safe = json.loads((root / "tests/fixtures/xray-template.json").read_text(encoding="utf-8"))
unsafe = copy.deepcopy(safe)
unsafe["log"]["error"] = ""


def database(path, template):
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE settings(key TEXT, value TEXT)")
        if template is not None:
            db.execute("INSERT INTO settings VALUES (?, ?)", ("xrayTemplateConfig", json.dumps(template)))
        db.commit()


def run(code, env):
    shim = 'python3(){ "$TEST_PYTHON" "$@"; }; export -f python3;\n'
    if os.name == "nt":
        # Production Linux Path strings are POSIX; adapt only the mock host's
        # Python transaction-directory stdout to the Git Bash filesystem.
        shim = '''python3(){
 if [[ "$1" == */transaction_state.py && "${2:-}" == begin ]]; then
   local output; output="$("$TEST_PYTHON" "$@")" || return $?; cygpath -u "$output"
 else "$TEST_PYTHON" "$@"; fi
}; export -f python3;
'''
    env = {key: value.replace("\\", "/") for key, value in env.items()}
    if os.name == "nt":
        shim += "".join(f'export {key}="$(cygpath -u "${key}")";\n' for key, value in env.items() if len(value) > 2 and value[1:3] == ":/")
    result = subprocess.run([bash, "-c", shim + code], env={**os.environ, **env, "LIB": (root / "scripts/lib").as_posix(), "TEST_PYTHON": sys.executable},
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stderr


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    safe_file = temp / "safe.json"
    safe_file.write_text(json.dumps(safe), encoding="utf-8")
    unsafe_file = temp / "unsafe.json"
    unsafe_file.write_text(json.dumps(unsafe), encoding="utf-8")
    default = copy.deepcopy(safe)
    default["log"] = {"access": "none", "error": "", "loglevel": "warning", "dnsLog": False, "maskAddress": ""}
    default_file = temp / "default.json"
    default_file.write_text(json.dumps(default), encoding="utf-8")
    # Exercise actual Exit commands, not only the role gate. Native getter's
    # effective response may come from a stored setting or embedded default.
    for origin in ("stored", "default"):
        for operation in ("create", "remove"):
            case = temp / f"{origin}-{operation}"
            case.mkdir()
            env = {"CASE": str(case), "EFFECTIVE": str(unsafe_file if origin == "stored" else default_file), "NODE_ROLE": "exit",
                   "COMMANDS": str(root / "scripts/commands"), "WM_STATE_DIR": str(case),
                   "WM_TRANSACTION_ROOT": str(case / "transactions")}
            run('''
set -Eeuo pipefail
source "$LIB/transaction.sh"; source "$LIB/xray_template.sh"; source "$COMMANDS/exit_peer.sh"
wm_fail(){ exit 42; }; wm_warn(){ :; }; wm_lock_mutation(){ :; }; wm_load_config(){ :; }
wm_xray_get_template(){ cp "$EFFECTIVE" "$1"; }
wm_inbound_reconcile(){ touch "$CASE/inbound-write"; }; wm_inbound_delete(){ touch "$CASE/inbound-write"; }
wm_nginx_apply_desired(){ touch "$CASE/service-write"; }
set +e
(
 if [[ "$OP" == create ]]; then
   wm_exit_peer_create --entry-id test-entry --display-name Test --output "$CASE/peer.json"
 else
   wm_exit_peer_remove --entry-id test-entry
 fi
)
rc=$?
[[ "$rc" == 42 && ! -e "$CASE/transactions" && ! -e "$CASE/inbound-write" && ! -e "$CASE/service-write" ]]
''', {**env, "OP": operation})

    # Full safe Exit snapshot, including SQLite backup, persists the exact
    # effective template even if the Entry subscription opt-out is inherited.
    case = temp / "safe-snapshot"
    case.mkdir()
    db = case / "live.db"
    database(db, safe)
    config = case / "config.json"
    config.write_text(json.dumps({"node": {"role": "exit"}, "installation": {"xui": {"database_path": db.as_posix()}}}), encoding="utf-8")
    base = {"CASE": str(case), "EFFECTIVE": str(safe_file), "WM_STATE_DIR": str(case), "NODE_ROLE": "exit",
            "WM_TRANSACTION_ROOT": str(case / "transactions"), "WM_CONFIG_JSON": str(config),
            "WM_RUNTIME_JSON": str(case / "runtime.json"), "WM_NGINX_MANAGED_CONF": str(case / "nginx.conf"),
            "WM_SUB_DIR": str(case / "subscriptions"), "WM_XRAY_SNAPSHOT_REQUIRED": "0"}
    run('''
set -Eeuo pipefail
source "$LIB/transaction.sh"; source "$LIB/xray_template.sh"
wm_fail(){ echo "$*" >&2; exit 42; }; wm_warn(){ :; }
wm_xray_get_template(){ cp "$EFFECTIVE" "$1"; }
wm_transaction_begin exit-peer-create
[[ -f "$WM_ACTIVE_TRANSACTION/xray.before.json" && -f "$WM_ACTIVE_TRANSACTION/x-ui.before.db" ]]
cmp "$EFFECTIVE" "$WM_ACTIVE_TRANSACTION/xray.before.json"
wm_transaction_commit
''', base)

    entry_tx = temp / "entry-subscription"
    entry_tx.mkdir()
    entry_config = temp / "entry-config.json"
    entry_config.write_text(json.dumps({"node": {"role": "entry"}, "installation": {"xui": {"database_path": db.as_posix()}}}), encoding="utf-8")
    run('''
set -Eeuo pipefail
source "$LIB/transaction.sh"; source "$LIB/xray_template.sh"
WM_XRAY_PREFLIGHT_FILE="$TX/preflight.json"; cp "$EFFECTIVE" "$WM_XRAY_PREFLIGHT_FILE"
wm_transaction_snapshot "$TX"
[[ ! -e "$TX/xray.before.json" ]]
wm_transaction_assert_xray_recovery "$TX"
''', {**base, "NODE_ROLE": "entry", "TX": str(entry_tx), "WM_CONFIG_JSON": str(entry_config)})

    # Safe effective snapshot cannot authorize unsafe/missing stored SQLite
    # template. Snapshot fails before the caller's first inbound write.
    for label, stored in (("unsafe", unsafe), ("default", None)):
        rejected_db = temp / f"snapshot-{label}.db"
        database(rejected_db, stored)
        rejected_config = temp / f"snapshot-{label}.json"
        rejected_config.write_text(json.dumps({"node": {"role": "exit"}, "installation": {"xui": {"database_path": rejected_db.as_posix()}}}), encoding="utf-8")
        tx = temp / f"snapshot-{label}"
        tx.mkdir()
        run('''
set -Eeuo pipefail
source "$LIB/transaction.sh"; source "$LIB/xray_template.sh"
wm_warn(){ :; }; WM_XRAY_PREFLIGHT_FILE="$EFFECTIVE"
if wm_transaction_snapshot "$TX"; then touch "$TX/inbound-write"; exit 20; fi
[[ ! -e "$TX/inbound-write" ]]
''', {**base, "WM_CONFIG_JSON": str(rejected_config), "EFFECTIVE": str(safe_file), "TX": str(tx)})
        # Snapshot consumes its private preflight file; recreate test input.
        safe_file.write_text(json.dumps(safe), encoding="utf-8")

    # Actual rollback ordering: failed guards cannot install live config/DB,
    # stop/start any service, or POST a template. Role comes from saved config,
    # since transaction CLI begins with its default standalone role.
    different_safe = copy.deepcopy(safe)
    different_safe["routing"]["domainStrategy"] = "IPIfNonMatch"
    variants = [("unsafe-snapshot", unsafe, safe), ("missing-snapshot", None, safe),
                ("unsafe-db", safe, unsafe), ("default-db", safe, None),
                ("different-safe-db", safe, different_safe),
                ("unknown-role", None, safe),
                ("safe", safe, safe)]
    for label, snapshot, stored in variants:
        tx = temp / f"rollback-{label}"
        tx.mkdir()
        (tx / "config.before.json").write_text(json.dumps({"node": {"role": "exit"}}), encoding="utf-8")
        (tx / "plan.json").write_text('{"operation":"exit-peer-remove"}', encoding="utf-8")
        if label == "unknown-role":
            (tx / "config.before.json").unlink()
            (tx / "plan.json").unlink()
        (tx / "subscriptions.before.absent").touch()
        if snapshot is not None:
            (tx / "xray.before.json").write_text(json.dumps(snapshot), encoding="utf-8")
        database(tx / "x-ui.before.db", stored)
        (tx / "x-ui.before.db.path").write_text((tx / "live.db").as_posix(), encoding="utf-8")
        before = (tx / "x-ui.before.db").read_bytes()
        run('''
set -Eeuo pipefail
source "$LIB/transaction.sh"; source "$LIB/xray_template.sh"
wm_warn(){ :; }; wm_load_config(){ :; }
wm_atomic_install_json(){ printf '%s\\n' config >> "$TX/writes"; cp "$1" "$2"; }
install(){ printf '%s\\n' database >> "$TX/writes"; cp "$3" "$4"; }
systemctl(){ printf '%s\\n' service >> "$TX/writes"; }
nginx(){ printf '%s\\n' nginx >> "$TX/writes"; }
wm_transaction_wait_xui(){ :; }
wm_xray_get_template(){ cp "$TX/xray.before.json" "$1"; }
wm_xui_request_success(){ printf '%s\\n' api >> "$TX/writes"; }
WM_CONFIG_JSON="$TX/live-config.json"; WM_RUNTIME_JSON="$TX/runtime.json"; WM_SUB_DIR="$TX/subscriptions"
if [[ "$EXPECT" == safe ]]; then
 wm_transaction_rollback "$TX" test
 [[ -f "$TX/writes" ]]
else
 if wm_transaction_rollback "$TX" test; then exit 21; fi
 [[ ! -e "$TX/writes" ]]
fi
''', {**base, "TX": str(tx), "EXPECT": label, "NODE_ROLE": "standalone"})
        assert (tx / "x-ui.before.db").read_bytes() == before
        result = json.loads((tx / "result.json").read_text(encoding="utf-8"))
        assert result["status"] == ("rolled_back" if label == "safe" else "rollback_failed")
        if label == "safe":
            writes = (tx / "writes").read_text(encoding="utf-8").splitlines()
            assert writes.index("config") < writes.index("database") < writes.index("api")
            with closing(sqlite3.connect(tx / "live.db")) as db:
                assert json.loads(db.execute("SELECT value FROM settings").fetchone()[0]) == safe
            assert json.loads((tx / "xray.rollback.readback.json").read_text(encoding="utf-8")) == safe

    # Explicitly saved standalone role retains generic non-Xray DB recovery;
    # absence of a saved role cannot inherit standalone from the fresh CLI.
    standalone = temp / "standalone"
    standalone.mkdir()
    (standalone / "config.before.json").write_text('{"node":{"role":"standalone"}}', encoding="utf-8")
    database(standalone / "x-ui.before.db", None)
    (standalone / "x-ui.before.db.path").write_text("fixture", encoding="utf-8")
    run('''
set -Eeuo pipefail
source "$LIB/transaction.sh"
wm_transaction_assert_xray_recovery "$TX"
''', {**base, "TX": str(standalone), "NODE_ROLE": "standalone"})

    dispatch = (root / "bin/wavemesh").read_text(encoding="utf-8").split("  exit)", 1)[1].split("  transaction)", 1)[0]
    assert 'source "$WM_LIB_DIR/lib/xray_template.sh"' in dispatch

print("exit log policy gate tests: OK")
