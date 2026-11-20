"""Native installed Bash/FD9/journal proof; synthetic collector/owner, no VPN.

Linux root only, isolated admitted VM. The test-only python function redirects
the installed adapter/abort process to synthetic evidence, never production code.
No fixture flag or alternate collector path is added to the product.
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
sys.path.insert(0, str(ROOT / "scripts/lib"))
import transaction_state as journal
from tests.unit import test_prepared_intent as fixtures


ADAPTER = '''import copy,json,os,sys
from contextlib import contextmanager,ExitStack
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(os.environ['SOURCE'])/'scripts/lib'))
import prepared_execution as execution,prepared_evaluation as evaluation
import prepared_reconcile as caller,prepared_owner as owner,transaction_state as journal
root=Path(os.environ['CASE_ROOT']); mode=os.environ['CASE_KIND']
view=json.loads((root/'view.json').read_text()); effective=json.loads((root/'effective.json').read_text())
@contextmanager
def collected(*,stable):
 if mode=='missing-collector':raise execution.ExecutionUnavailable('synthetic collector absent')
 yield ((copy.deepcopy(view),copy.deepcopy(effective)),object(),lambda:None)
def pending(directory,credential):
 (root/'post.marker').write_text('post\\n')
 journal.atomic_json(directory/'prepared.owner-request.json',{'synthetic':'one HTTP success'})
 journal.atomic_json(directory/'prepared.readback.json',effective)
 return 'pending'
atomic=journal.atomic_json
def stopped(path,value):
 atomic(path,value)
 if mode=='pre-effect-fail' and Path(path).name=='plan.json' and value.get('prepared_phase')=='pre-effect':
  raise OSError('synthetic pre-effect crash')
stage=evaluation._stage_collected
def changed(*args):
 result,view,effective=stage(*args)
 if mode=='config-only':result['candidate']['config']['foreign_change']=True
 return result,view,effective
with ExitStack() as stack:
 stack.enter_context(mock.patch.object(execution,'_collected_evidence',side_effect=collected))
 stack.enter_context(mock.patch.object(evaluation.time,'time',return_value=110))
 stack.enter_context(mock.patch.object(owner,'apply_once',side_effect=pending))
 stack.enter_context(mock.patch.object(journal,'atomic_json',side_effect=stopped))
 stack.enter_context(mock.patch.object(evaluation,'_stage_collected',side_effect=changed))
 if sys.argv[1]=='abort':
  try:journal.abort_prepared(sys.argv[2])
  except (OSError,ValueError,RuntimeError):raise SystemExit(1)
 else:
  sys.argv=sys.argv[1:]
  caller.main()
'''


class PreparedOwningTransaction(unittest.TestCase):
    def run_case(self, mode, supplied=None):
        if sys.platform != "linux" or not shutil.which("bash") or os.geteuid() != 0:
            raise RuntimeError("native Linux root Bash required; run in isolated admitted VM")
        fixture = fixtures.PreparedIntent()
        fixture.setUp()
        try:
            with tempfile.TemporaryDirectory(prefix="wm-owning-shell-") as temporary:
                root = Path(temporary)
                observations = fixture.case.observations
                if mode not in ("update", "pre-effect-fail"):
                    observations["paths"] = dict.fromkeys(observations["paths"], False)
                for name, value in (("observations.json", observations), ("view.json", fixture.case.view),
                                    ("effective.json", fixture.effective)):
                    journal.atomic_json(root / name, value)
                (root / "adapter.py").write_text(ADAPTER, encoding="utf-8")
                script = '''set -Eeuo pipefail
WM_LIB_DIR="$SOURCE/scripts"
WM_STATE_DIR="$CASE_ROOT/state"
WM_TRANSACTION_ROOT="$WM_STATE_DIR/transactions"
WM_CONFIG_JSON="$CONFIG_FILE"
WM_RUNTIME_JSON="$RUNTIME_FILE"
WM_SUB_DIR="$CASE_ROOT/subscriptions"
NODE_ROLE=entry
mkdir -p "$WM_STATE_DIR" "$WM_SUB_DIR"
source "$WM_LIB_DIR/lib/transaction.sh"
source "$WM_LIB_DIR/commands/auto.sh"
wm_warn(){ :; }
wm_success(){ :; }
wm_fail(){ printf '%s\\n' "$*" >&2; exit 42; }
wm_load_config(){ :; }
wm_require_entry_role(){ [[ "$NODE_ROLE" == entry ]]; }
wm_xray_policy_preflight(){ :; }
# No generic snapshot or service/template operation is permitted on this path.
wm_transaction_snapshot(){ printf snapshot >> "$CASE_ROOT/forbidden.marker"; return 1; }
wm_xray_apply_template(){ printf legacy-post >> "$CASE_ROOT/forbidden.marker"; return 1; }
systemctl(){ printf service >> "$CASE_ROOT/forbidden.marker"; return 1; }
if [[ "$CASE_KIND" == missing-fd9 ]]; then wm_lock_mutation(){ :; }; fi
python3(){
 if [[ "${1:-}" == "$WM_LIB_DIR/lib/prepared_reconcile.py" ]]; then
  command python3 "$CASE_ROOT/adapter.py" "$@"
 elif [[ "${1:-}" == "$WM_TRANSACTION_TOOL" && "${2:-}" == prepared-abort ]]; then
  command python3 "$CASE_ROOT/adapter.py" abort "$4"
 else command python3 "$@"; fi
}
extra=()
[[ -z "$SUPPLIED_VIEW" ]] || extra+=("$SUPPLIED_VIEW" "$CASE_ROOT/view.json")
wm_auto_prepared reconcile --id europe --database "$DATABASE_FILE" --process-config "$PROCESS_FILE" \
 --credential-file "$CASE_ROOT/unused-synthetic-bearer.json" --observations "$CASE_ROOT/observations.json" "${extra[@]}"
'''
                env = {**os.environ, "SOURCE": str(ROOT), "CASE_ROOT": str(root), "CASE_KIND": mode,
                       "SUPPLIED_VIEW": supplied or "", **{key.upper() + "_FILE": value for key, value in fixture.files.items()}}
                result = subprocess.run(["bash", "--noprofile", "--norc", "-c", script], env=env,
                                        capture_output=True, text=True, timeout=60)
                records = journal.records(root / "state/transactions")
                transaction = root / "state/transactions" / records[0]["id"] if records else None
                raw = json.loads((transaction / "result.json").read_text()) if transaction else None
                return {"code": result.returncode, "stderr": result.stderr, "records": records, "raw": raw,
                    "pending": bool(transaction and (transaction / "prepared.apply.pending.json").exists()),
                    "readback": bool(transaction and (transaction / "prepared.readback.json").exists()),
                    "post": (root / "post.marker").read_text().splitlines() if (root / "post.marker").exists() else [],
                    "forbidden": (root / "forbidden.marker").exists(),
                    "unchanged": {key: Path(path).read_bytes() == fixture.before[key] for key, path in fixture.files.items()},
                    "runtime": json.loads(Path(fixture.files["runtime"]).read_text())}
        finally:
            fixture.doCleanups()

    def test_noop_real_shell_fd9_updates_health_without_post_snapshot_or_service(self):
        result = self.run_case("noop")
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["post"], [])
        self.assertFalse(result["forbidden"])
        self.assertEqual(result["records"][0]["status"], "committed")
        self.assertIn("prepared_health_commit", result["raw"])
        self.assertTrue(all(result["unchanged"][key] for key in ("config", "database", "process")))
        self.assertFalse(result["unchanged"]["runtime"])
        self.assertEqual(result["runtime"]["unowned"], {"keep": True})

    def test_http_success_cache_readback_cannot_settle_or_restore(self):
        result = self.run_case("update")
        self.assertEqual(result["code"], 42)
        self.assertEqual(result["post"], ["post"])
        self.assertTrue(result["pending"] and result["readback"])
        self.assertFalse(result["forbidden"])
        self.assertEqual(result["records"][0]["status"], "rollback_failed")
        self.assertTrue(all(result["unchanged"].values()))

    def test_pre_effect_failure_closes_only_verified_no_effect_without_restore(self):
        result = self.run_case("pre-effect-fail")
        self.assertEqual(result["code"], 42)
        self.assertEqual(result["post"], [])
        self.assertEqual(result["records"][0]["status"], "rolled_back")
        self.assertIn("prepared_no_effect", result["raw"])
        self.assertFalse(result["forbidden"])
        self.assertTrue(all(result["unchanged"].values()))

    def test_missing_fd9_cannot_begin_prepared_transaction(self):
        result = self.run_case("missing-fd9")
        self.assertEqual(result["code"], 42)
        self.assertEqual(result["records"], [])
        self.assertEqual(result["post"], [])
        self.assertFalse(result["forbidden"])
        self.assertTrue(all(result["unchanged"].values()))

    def test_missing_collector_or_config_drift_keeps_debt_without_mutation(self):
        for mode in ("missing-collector", "config-only"):
            with self.subTest(mode=mode):
                result = self.run_case(mode)
                self.assertEqual(result["code"], 42)
                self.assertEqual(result["post"], [])
                self.assertEqual(result["records"][0]["status"], "rollback_failed")
                self.assertFalse(result["forbidden"])
                self.assertTrue(all(result["unchanged"].values()))

    def test_supplied_execution_views_refuse_before_transaction(self):
        for supplied in ("--runtime-view", "--effective-config"):
            with self.subTest(supplied=supplied):
                result = self.run_case("noop", supplied)
                self.assertEqual(result["code"], 42)
                self.assertEqual(result["records"], [])
                self.assertEqual(result["post"], [])
                self.assertFalse(result["forbidden"])
                self.assertTrue(all(result["unchanged"].values()))


if __name__ == "__main__":
    unittest.main()
