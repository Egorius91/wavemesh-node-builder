import os
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
bash = shutil.which("bash") or next(
    (path for path in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe") if Path(path).is_file()),
    None,
)
if not bash:
    raise SystemExit("bash is required for Xray policy gate tests")

with tempfile.TemporaryDirectory() as name:
    temp = Path(name)
    state = temp / "state"
    transaction_root = state / "transactions"
    posted = temp / "post-called"
    bash_env = temp / "bash-env.sh"
    bash_env.write_text('python3(){ "' + Path(sys.executable).as_posix() + '" "$@"; }; export -f python3;\n', encoding="utf-8")
    env = {
        **os.environ,
        "NODE_ROLE": "entry",
        "WM_STATE_DIR": str(state),
        "WM_TRANSACTION_ROOT": str(transaction_root),
        "POST_MARKER": str(posted),
        "BASH_ENV": str(bash_env),
    }

    # Transaction preflight rejects before creating its transaction directory.
    preflight = subprocess.run(
        [bash, "-c", 'source "$LIB/transaction.sh"; wm_fail(){ exit 42; }; wm_xray_policy_preflight(){ return 1; }; wm_transaction_begin route-enable'],
        env={**env, "LIB": str(root / "scripts/lib")},
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert preflight.returncode == 42
    assert not transaction_root.exists()

    # Both direct route mutation and the last Xray POST gate reject before any
    # backup or panel update can occur.
    gate = subprocess.run(
        [bash, "-c", '''
source "$LIB/xray_template.sh"
wm_warn(){ :; }
wm_xray_assert_log_policy(){ return 1; }
wm_xui_request_success(){ printf called > "$POST_MARKER"; }
wm_xray_apply_template "$CANDIDATE" && exit 11 || :
wm_xray_apply_managed_route "$OUTBOUND" wm-route-test wm-exit-test wm-rule-test && exit 12 || :
'''],
        env={
            **env,
            "LIB": str(root / "scripts/lib"),
            "CANDIDATE": str(root / "tests/fixtures/xray-template.json"),
            "OUTBOUND": str(root / "tests/fixtures/xray-outbound-de.json"),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert gate.returncode == 0, gate.stderr
    assert not posted.exists()
    assert not (state / "backups").exists()

    # An unsafe saved transaction snapshot is retained and never replayed.
    legacy_transaction = temp / "legacy-transaction"
    legacy_transaction.mkdir()
    unsafe_snapshot = {
        "log": {
            "access": "/private/legacy-access.log",
            "error": "/private/legacy-error.log",
            "loglevel": "warning",
            "dnsLog": False,
            "maskAddress": "full",
        }
    }
    snapshot = legacy_transaction / "xray.before.json"
    snapshot.write_text(json.dumps(unsafe_snapshot), encoding="utf-8")
    mock_tool = temp / "transaction_state_mock.py"
    mock_tool.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    rollback = subprocess.run(
        [bash, "-c", '''
source "$LIB/transaction.sh"
source "$LIB/xray_template.sh"
WM_TRANSACTION_TOOL="$MOCK_TOOL"
wm_warn(){ :; }
wm_xui_request_success(){ printf called > "$POST_MARKER"; }
if wm_transaction_rollback "$TX" unsafe; then exit 21; fi
[[ -f "$TX/xray.before.json" ]]
[[ ! -e "$POST_MARKER" ]]
'''],
        env={
            **env,
            "LIB": str(root / "scripts/lib"),
            "TX": str(legacy_transaction),
            "MOCK_TOOL": str(mock_tool),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert rollback.returncode == 0, rollback.stderr
    assert snapshot.exists() and not posted.exists()

    # Subscription transactions perform the Entry preflight but do not retain
    # or restore an unrelated Xray template.
    subscription_tx = temp / "subscription-transaction"
    subscription_tx.mkdir()
    preflight_file = temp / "safe-preflight.json"
    preflight_file.write_text(
        (root / "tests/fixtures/xray-template.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    config_file = temp / "config.json"
    config_file.write_text('{"installation":{"xui":{"database_path":""}}}\n', encoding="utf-8")
    subdir = temp / "subscriptions"
    subdir.mkdir()
    snapshot_result = subprocess.run(
        [bash, "-c", '''
source "$LIB/transaction.sh"
source "$LIB/xray_template.sh"
NODE_ROLE=entry
WM_XRAY_SNAPSHOT_REQUIRED=0
WM_XRAY_PREFLIGHT_FILE="$PREFLIGHT"
WM_CONFIG_JSON="$CONFIG"
WM_RUNTIME_JSON="$RUNTIME"
WM_NGINX_MANAGED_CONF="$NGINX"
WM_SUB_DIR="$SUBDIR"
wm_transaction_snapshot "$TX"
[[ ! -f "$TX/xray.before.json" ]]
[[ ! -e "$PREFLIGHT" ]]
'''],
        env={
            **env,
            "LIB": str(root / "scripts/lib"),
            "TX": str(subscription_tx),
            "PREFLIGHT": str(preflight_file),
            "CONFIG": str(config_file),
            "RUNTIME": str(temp / "runtime.json"),
            "NGINX": str(temp / "nginx.conf"),
            "SUBDIR": str(subdir),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert snapshot_result.returncode == 0, snapshot_result.stderr
    assert not (subscription_tx / "xray.before.json").exists()
    assert not preflight_file.exists()

    dispatch = (root / "bin/wavemesh").read_text(encoding="utf-8")
    subscription_case = dispatch.split("  subscription)", 1)[1].split("  exit)", 1)[0]
    assert 'source "$WM_LIB_DIR/lib/xray_template.sh"' in subscription_case
    assert "WM_XRAY_SNAPSHOT_REQUIRED=0" in subscription_case
    subscription_commands = (root / "scripts/commands/subscription.sh").read_text(encoding="utf-8")
    rebuild = subscription_commands.split("wm_subscription_rebuild_command() {", 1)[1].split("wm_subscription_validate_command() {", 1)[0]
    assert rebuild.index('wm_transaction_begin "subscription-rebuild"') < rebuild.index("wm_apply_subscription_presentation")

print("xray private log gate tests: OK")
