"""Actual Bash/flock exclusion for every persisted-health entry path."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("health_access_runtime", ROOT / "agent/access_runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


@unittest.skipUnless(os.name == "posix" and shutil.which("bash") and shutil.which("flock"),
                     "Requires native Bash and flock")
class HealthWriterLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wm-health-lock-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.lock = self.directory / "node.lock"
        self.state = self.directory / "runtime.json"
        self.state.write_text('{"sentinel":true}', encoding="utf-8")
        (self.directory / "config.json").write_text('{"routes":[],"exits":[]}', encoding="utf-8")
        # Only the fixed production lock pathname is redirected. Real lock code,
        # health implementation, evaluator and dispatch functions stay intact.
        source = (ROOT / "scripts/lib/transaction.sh").read_text(encoding="utf-8")
        source = source.replace("/run/lock/wavemesh-node.lock", str(self.lock))
        # Unit fixtures must not require or alter a host-owned runtime directory.
        source = source.replace("mkdir -p /run/lock", ":")
        (self.directory / "transaction.sh").write_text(source, encoding="utf-8")
        self.script = r'''
set -Eeuo pipefail
fixture="$1"; WM_LIB_DIR="$2/scripts"
TMPDIR="$fixture"; export TMPDIR
WM_STATE_DIR="$fixture"; WM_CONFIG_JSON="$fixture/config.json"
WM_RUNTIME_JSON="$fixture/runtime.json"; WM_SUB_DIR="$fixture/subscriptions"
source "$fixture/transaction.sh"
WM_TRANSACTION_TOOL="$WM_LIB_DIR/lib/transaction_state.py"
source "$WM_LIB_DIR/commands/runtime.sh"
source "$WM_LIB_DIR/commands/cascade.sh"
wm_fail(){ echo "$*" >&2; exit 42; }
assert_locked(){
  if flock -n "$fixture/node.lock" true; then wm_fail "writer lock not held"; fi
  stat -c '%i' "$fixture/node.lock" >> "$fixture/inodes"
}
wm_load_config(){ assert_locked; NODE_ROLE=entry; }
wm_xui_request_success(){ assert_locked; printf '{"obj":[]}\n'; }
wm_xray_get_template(){ assert_locked; printf '{}\n' > "$1"; }
wm_runtime_control_probe(){ assert_locked; printf '{"control":{},"routes":[]}\n' > "$1"; }
wm_subscription_backend(){ printf 'xui-native\n'; }
wm_native_validate_public(){ :; }
# Reconcile has no routes in this fixture. Isolate external mutation stages,
# but check their lock ownership and execute real health/evaluate before/after.
wm_transaction_begin(){ assert_locked; WM_ACTIVE_TRANSACTION="$fixture/candidate"; mkdir "$WM_ACTIVE_TRANSACTION"; }
wm_native_apply_settings(){ assert_locked; }
wm_runtime_prepare_subscriptions(){ assert_locked; WM_RUNTIME_CANDIDATE="$2/config.subscription.json"; cp "$1" "$WM_RUNTIME_CANDIDATE"; }
wm_runtime_apply_public_state(){ assert_locked; }
wm_atomic_install_json(){ assert_locked; cp "$1" "$2"; }
wm_export_config_env_from_json(){ assert_locked; }
wm_transaction_commit(){ assert_locked; }
# Real health/evaluate is exercised by the three public health paths below.
# Reconcile tests its ownership across delegation and external mutation stages;
# avoid repeating that probe fixture twice in the same bounded subprocess.
if [[ ! -d "$fixture/transactions" && ( "$3" == dry || "$3" == apply ) ]]; then
  wm_runtime_health_locked(){ wm_load_config; assert_locked; printf '{"observed_at":"reconcile-fixture"}\n' > "$WM_RUNTIME_JSON"; }
fi
case "$3" in
  health) wm_runtime_health --json; flock -n "$fixture/node.lock" true;;
  cascade) wm_cascade_command health --json; flock -n "$fixture/node.lock" true;;
  verify) # verify-e2e loads config before invoking the health writer.
    wm_load_config(){ NODE_ROLE=entry; }
    wm_cascade_verify_e2e --json; flock -n "$fixture/node.lock" true;;
  dry) wm_reconcile_command --dry-run; assert_locked;;
  apply) wm_reconcile_command --apply;;
esac
'''

    def invoke(self, mode):
        args = ["bash", "-c", self.script, "fixture", str(self.directory), str(ROOT), mode]
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True) as process:
            try:
                stdout, stderr = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
            return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)

    def test_agent_lock_refuses_every_health_writer_before_state_change(self):
        before = self.state.read_bytes()
        with runtime.node_mutation_lock(self.lock):
            inode = self.lock.stat().st_ino
            for mode in ("health", "cascade", "verify", "dry", "apply"):
                with self.subTest(mode=mode):
                    result = self.invoke(mode)
                    self.assertEqual(result.returncode, 42, result.stderr)
                    self.assertIn("Another WaveMesh mutation is running", result.stderr)
                    self.assertEqual(self.state.read_bytes(), before)
                    self.assertFalse((self.directory / "inodes").exists())
                    self.assertEqual(self.lock.stat().st_ino, inode)

    def test_health_and_reconcile_hold_node_lock_through_persistence_and_release(self):
        for mode in ("health", "cascade", "verify", "dry", "apply"):
            with self.subTest(mode=mode):
                (self.directory / "inodes").unlink(missing_ok=True)
                result = self.invoke(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("observed_at", json.loads(self.state.read_text()))
                inodes = (self.directory / "inodes").read_text().splitlines()
                self.assertGreaterEqual(len(inodes), 3)
                self.assertEqual(set(inodes), {str(self.lock.stat().st_ino)})
                with runtime.node_mutation_lock(self.lock):
                    pass

    def test_prepared_debt_refuses_every_health_writer_after_dead_process_releases_lock(self):
        directory = self.directory / "transactions" / "retained"
        directory.mkdir(parents=True)
        (directory / "plan.json").write_text('{"schema_version":1,"operation":"auto-prepared-reconcile"}')
        result = directory / "result.json"
        result.write_text('{"status":"committed"}')
        before = self.state.read_bytes()
        for origin in ("pending", "completed", "proof-only"):
            marker = directory / f"prepared.apply.{origin}.json"
            if origin == "pending":
                marker.symlink_to(directory / "missing")
            elif origin == "completed":
                marker.write_text("malformed")
            else:
                result.write_text('{"status":"committed","prepared_commit":{}}')
                result.chmod(0o600)
            for mode in ("health", "cascade", "verify", "dry", "apply"):
                with self.subTest(origin=origin, mode=mode):
                    completed = self.invoke(mode)
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertEqual(self.state.read_bytes(), before)
                    self.assertFalse((self.directory / "inodes").exists())
            marker.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
