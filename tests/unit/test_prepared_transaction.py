"""Native Bash transaction orchestration with a synthetic compiler/panel.

This is not an engine/collector/service proof. The actual compiler is tested
separately; here a successful mutable panel response must not clear transition
debt, and an unchanged tick must not dispatch a panel operation.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"tests/unit"))
from test_prepared_channels import fixture
sys.path.insert(0, str(ROOT/"scripts/lib"))
import prepared_channels


class PreparedTransaction(unittest.TestCase):
    def run_case(self, case, include_view=True, include_effective=True):
        if sys.platform != "linux" or not shutil.which("bash"):
            raise RuntimeError("native Linux Bash required; run in isolated VM")
        with tempfile.TemporaryDirectory(prefix="wm-prepared-transaction-") as temporary:
            directory = Path(temporary)
            config, plan, template = fixture()
            config, runtime, template, _ = prepared_channels.stage(config, {}, "route-auto-europe", plan, template, now=100)
            config.setdefault("installation", {}).setdefault("xui", {})["database_path"] = ""
            files = {"config.json": config, "runtime.json": runtime, "template.json": template, "plan.json": plan, "view.json": {}, "observation.json": {}}
            for name, value in files.items(): (directory/name).write_text(json.dumps(value))
            original_config = (directory/"config.json").read_bytes()
            original_runtime = (directory/"runtime.json").read_bytes()
            compiler = directory/"compiler.py"
            compiler.write_text('''import json,os,sys
from pathlib import Path
args=sys.argv; output=Path(args[args.index('--output')+1]); root=Path(os.environ['CASE_ROOT']); case=os.environ['CASE_KIND']
if case=='compile-fail':sys.exit(9)
config=json.loads((root/'config.json').read_text()); runtime=json.loads((root/'runtime.json').read_text()); template=json.loads((root/'template.json').read_text())
runtime['test_health_receipt']='new-qualified-source-fixture'
if case=='config-only':config['test_metadata_change']=True
if case=='update':template['test_candidate_change']=True
for name,value in [('prepared.config.json',config),('prepared.runtime.json',runtime),('prepared.xray.json',template),('prepared.update.json',{'update_required':case=='update'})]:
 (output/name).write_text(json.dumps(value))
''')
            script = '''set -Eeuo pipefail
WM_LIB_DIR="$SOURCE/scripts"
WM_STATE_DIR="$CASE_ROOT/state"
WM_CONFIG_JSON="$CASE_ROOT/config.json"
WM_RUNTIME_JSON="$CASE_ROOT/runtime.json"
WM_SUB_DIR="$CASE_ROOT/subscriptions"
WM_NGINX_MANAGED_CONF="$CASE_ROOT/nginx.conf"
WM_TRANSACTION_ROOT="$WM_STATE_DIR/transactions"
NODE_ROLE=entry
mkdir -p "$WM_STATE_DIR" "$WM_SUB_DIR"
source "$WM_LIB_DIR/lib/transaction.sh"
source "$WM_LIB_DIR/lib/xray_template.sh"
source "$WM_LIB_DIR/commands/auto.sh"
WM_PREPARED_TOOL="$CASE_ROOT/compiler.py"
wm_warn(){ :; }
wm_success(){ :; }
wm_fail(){ printf '%s\n' "$*" >&2; exit 42; }
wm_lock_mutation(){ :; }
wm_load_config(){ :; }
wm_require_entry_role(){ [[ "$NODE_ROLE" == entry ]]; }
wm_xray_get_template(){ cp "$CASE_ROOT/template.json" "$1"; }
wm_xray_apply_template(){ printf 'post\n' >> "$CASE_ROOT/post.marker"; }
wm_export_config_env_from_json(){ printf 'export\n' >> "$CASE_ROOT/export.marker"; }
wm_auto_prepared reconcile --id europe --observations "$CASE_ROOT/observation.json" VIEW_ARGUMENT EFFECTIVE_ARGUMENT
'''.replace("VIEW_ARGUMENT", '--runtime-view "$CASE_ROOT/view.json"' if include_view else "").replace("EFFECTIVE_ARGUMENT", '--effective-config "$CASE_ROOT/template.json"' if include_effective else "")
            result = subprocess.run(["bash", "--noprofile", "--norc", "-c", script], env={**os.environ, "CASE_ROOT": str(directory), "CASE_KIND": case, "SOURCE": str(ROOT)}, capture_output=True, text=True, timeout=60)
            transactions = list((directory/"state/transactions").glob("*")) if (directory/"state/transactions").exists() else []
            record = json.loads((transactions[0]/"result.json").read_text()) if transactions else None
            post = (directory/"post.marker").read_text().splitlines() if (directory/"post.marker").exists() else []
            return {"exitcode": result.returncode, "stderr": result.stderr, "post": post, "result": record,
                    "pending": bool(transactions and (transactions[0]/"prepared.apply.pending.json").exists()),
                    "readback": bool(transactions and (transactions[0]/"prepared.readback.json").exists()),
                    "config_unchanged": (directory/"config.json").read_bytes() == original_config,
                    "runtime_unchanged": (directory/"runtime.json").read_bytes() == original_runtime,
                    "runtime": json.loads((directory/"runtime.json").read_text())}

    def test_noop_tick_updates_only_health_without_post(self):
        result = self.run_case("noop")
        self.assertEqual(result["exitcode"], 0, result["stderr"])
        self.assertEqual(result["post"], [])
        self.assertEqual(result["result"]["status"], "committed")
        self.assertTrue(result["config_unchanged"])
        self.assertEqual(result["runtime"]["test_health_receipt"], "new-qualified-source-fixture")

    def test_http_success_and_cache_readback_keep_unknown_transition(self):
        result = self.run_case("update")
        self.assertEqual(result["exitcode"], 42)
        self.assertEqual(result["post"], ["post"])
        self.assertTrue(result["pending"])
        self.assertTrue(result["readback"])
        self.assertEqual(result["result"]["status"], "rollback_failed")
        self.assertTrue(result["runtime_unchanged"])
        self.assertTrue(result["config_unchanged"])

    def test_compile_refusal_does_not_restore_or_post(self):
        result = self.run_case("compile-fail")
        self.assertEqual(result["exitcode"], 42)
        self.assertEqual(result["post"], [])
        self.assertEqual(result["result"]["status"], "rolled_back")
        self.assertTrue(result["config_unchanged"] and result["runtime_unchanged"])

    def test_config_only_change_never_causes_unnecessary_engine_post(self):
        result = self.run_case("config-only")
        self.assertEqual(result["exitcode"], 42)
        self.assertEqual(result["post"], [])
        self.assertTrue(result["config_unchanged"] and result["runtime_unchanged"])

    def test_no_current_view_stops_before_transaction(self):
        result = self.run_case("noop", include_view=False)
        self.assertEqual(result["exitcode"], 42)
        self.assertIsNone(result["result"])
        self.assertEqual(result["post"], [])

    def test_no_actual_config_stops_before_transaction(self):
        result = self.run_case("noop", include_effective=False)
        self.assertEqual(result["exitcode"], 42)
        self.assertIsNone(result["result"])
        self.assertEqual(result["post"], [])


if __name__ == "__main__": unittest.main()
