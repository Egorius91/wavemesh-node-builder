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


    # Exercise the real output boundary with panel-controlled diagnostics.
    # Only HTTP transport is mocked; envelope parsing, probe parsing, warning
    # output and managed-route admission are the production functions.
    markers = (
        "9f887d8b-f621-4f18-8866-1f6af05d8a21",
        "/synthetic-xhttp-private-path",
        "synthetic-subscription-private-id",
    )
    private_error = " ".join(markers)
    probe_response = temp / "probe-response.json"
    probe_posted = temp / "probe-post-called"
    cases = (
        ("failed-probe", {"success": True, "obj": {"success": False, "error": private_error}}, 1),
        ("failed-envelope", {"success": False, "msg": private_error}, 1),
        ("transport-failure", {}, 1),
        ("unsupported-result", {"success": True, "obj": [private_error]}, 1),
        ("negative-delay", {"success": True, "obj": -1}, 1),
        ("success", {"success": True, "obj": {"success": True, "delay": 87, "error": private_error}}, 0),
    )
    for label, response, expected in cases:
        probe_response.write_text(json.dumps(response), encoding="utf-8")
        probe = subprocess.run(
            [bash, "-c", """
source "$COMMON"
source "$LIB/xui_api.sh"
source "$LIB/xray_template.sh"
wm_xui_request(){
  [[ "$1" == POST && "$2" == /panel/api/xray/testOutbound && "$3" == form && "$4" == *mode=real* ]] || return 42
  if [[ "$CASE" == transport-failure ]]; then wm_warn "3X-UI transport failed"; return 1; fi
  cat "$RESPONSE"
}
wm_xray_test_outbound "$OUTBOUND" "$CANDIDATE"
"""],
            env={
                **env, "LIB": (root / "scripts/lib").as_posix(),
                "COMMON": (root / "scripts/00_common.sh").as_posix(),
                "OUTBOUND": (root / "tests/fixtures/xray-outbound-de.json").as_posix(),
                "CANDIDATE": (root / "tests/fixtures/xray-template.json").as_posix(),
                "RESPONSE": probe_response.as_posix(), "CASE": label,
                "PRIVATE_ERROR": private_error,
            },
            check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert probe.returncode == expected, (label, probe.returncode)
        assert probe.stdout == "", label
        assert all(marker not in probe.stderr for marker in markers), label
        if label != "transport-failure" and label != "failed-envelope":
            cli = subprocess.run(
                [sys.executable, str(root / "scripts/lib/xray_response.py"),
                 "--kind", "test-outbound", "--response", str(probe_response)],
                check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            assert cli.returncode == expected, label
            assert all(marker not in cli.stdout + cli.stderr for marker in markers), label
            if label != "unsupported-result":
                projected = json.loads(cli.stdout)
                assert projected["success"] is (expected == 0), label
                assert projected["error"] == (None if expected == 0 else "outbound probe failed"), label
                if label == "success":
                    assert projected["delay"] == 87
        if label in ("success", "failed-envelope"):
            envelope = subprocess.run(
                [bash, "-c", """
source "$COMMON"
source "$LIB/xui_api.sh"
wm_xui_request(){ cat "$RESPONSE"; }
wm_xui_request_success POST /panel/api/xray/testOutbound form mode=real
"""],
                env={**env, "LIB": (root / "scripts/lib").as_posix(),
                     "COMMON": (root / "scripts/00_common.sh").as_posix(),
                     "RESPONSE": probe_response.as_posix()},
                check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            assert envelope.returncode == expected, label
            if expected == 0:
                assert json.loads(envelope.stdout) == response, label
                assert envelope.stderr == "", label
            else:
                assert envelope.stdout == "", label
                assert all(marker not in envelope.stderr for marker in markers), label
                assert envelope.stderr.strip().endswith("3X-UI operation failed"), label
        if expected:
            expected_warning = "3X-UI operation failed" if label == "failed-envelope" else ("3X-UI transport failed" if label == "transport-failure" else "3X-UI outbound data-plane probe failed")
            assert probe.stderr.strip().endswith(expected_warning), label
        else:
            assert probe.stderr == "", label


    # Run the production request helper with only curl replaced. Dynamic
    # subscription/client paths still reach HTTP transport, while its failure
    # warning contains only a controlled status and the fixed auth-mode enum.
    for request_path in (
        "/panel/api/clients/subLinks/" + markers[2],
        "/panel/api/clients/get/" + markers[0],
        "/panel/api/clients/update/" + markers[0] + markers[1],
    ):
        for status in ("404", "500", "", private_error, "200"):
            target_file = temp / "transport-target.txt"
            transport = subprocess.run(
                [bash, "-c", """
source "$COMMON"
source "$LIB/xui_api.sh"
PANEL_TOKEN="synthetic-test-token"
PANEL_PORT=50000
PANEL_PATH=/panel-test/
curl(){
  local output="" url="" method=""
  while (( $# )); do
    case "$1" in
      --output) output="$2"; shift 2 ;;
      --request) method="$2"; shift 2 ;;
      *) url="$1"; shift ;;
    esac
  done
  [[ "$method" == GET && "$url" == "http://127.0.0.1:50000/panel-test$REQUEST_PATH" ]] || return 42
  printf '%s' "$url" > "$TRANSPORT_TARGET"
  printf '{"success":true,"obj":{"value":"unchanged"}}' > "$output"
  printf '%s' "$HTTP_STATUS"
}
wm_xui_request GET "$REQUEST_PATH" none
"""],
                env={**env, "LIB": (root / "scripts/lib").as_posix(),
                     "COMMON": (root / "scripts/00_common.sh").as_posix(),
                     "REQUEST_PATH": request_path, "HTTP_STATUS": status,
                     "TRANSPORT_TARGET": target_file.as_posix()},
                check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            assert target_file.read_text(encoding="utf-8") == "http://127.0.0.1:50000/panel-test" + request_path
            assert all(marker not in transport.stdout + transport.stderr for marker in markers)
            if status == "200":
                assert transport.returncode == 0
                assert json.loads(transport.stdout) == {"success": True, "obj": {"value": "unchanged"}}
                assert transport.stderr == ""
            else:
                assert transport.returncode == 1
                assert transport.stdout == ""
                expected_status = status if status in ("404", "500") else "transport-error"
                assert transport.stderr.strip().endswith(f"3X-UI request failed with HTTP {expected_status} (bearer auth)")

    # A failed real probe must still prevent the candidate Xray update.
    probe_response.write_text(json.dumps(cases[0][1]), encoding="utf-8")
    route = subprocess.run(
        [bash, "-c", """
source "$COMMON"
source "$LIB/xui_api.sh"
source "$LIB/xray_template.sh"
WM_STATE_DIR="$TEST_STATE"
wm_xray_get_template(){ cp "$BASELINE" "$1"; }
wm_xui_request(){ cat "$RESPONSE"; }
wm_xray_apply_template(){ printf called > "$PROBE_POST_MARKER"; }
wm_xray_apply_managed_route "$OUTBOUND" wm-route-test wm-exit-de-fra-1 wm-rule-test
"""],
        env={
            **env, "LIB": (root / "scripts/lib").as_posix(),
            "COMMON": (root / "scripts/00_common.sh").as_posix(),
            "BASELINE": (root / "tests/fixtures/xray-template.json").as_posix(),
            "OUTBOUND": (root / "tests/fixtures/xray-outbound-de.json").as_posix(),
            "RESPONSE": probe_response.as_posix(), "TEST_STATE": (temp / "probe-state").as_posix(),
            "PROBE_POST_MARKER": probe_posted.as_posix(),
        },
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert route.returncode == 1
    assert not probe_posted.exists()
    assert route.stdout == ""
    assert all(marker not in route.stderr for marker in markers)

print("xray private log gate tests: OK")
