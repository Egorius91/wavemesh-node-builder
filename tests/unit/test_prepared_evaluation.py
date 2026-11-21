"""Source integration only: actual compiler with a synthetic private collector."""
import copy
from contextlib import ExitStack
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepared_channels as channels
import prepared_evaluation as evaluation
import prepared_execution as execution
import xray_template
from test_prepared_channels import fixture


class PreparedEvaluation(unittest.TestCase):
    def setUp(self):
        config, plan, template = fixture()
        engine = {"name": "xray", "version": "26.10.21", "sha256": execution.CORE}
        plan["channel"]["engine"] = engine
        for approval in plan["exits"]:
            approval["channel"]["engine"] = copy.deepcopy(engine)
        self.route_id = "route-auto-europe"
        self.config, self.runtime, self.template, self.binding = channels.stage(
            config, {}, self.route_id, plan, template, now=100)
        self.plan = plan
        self.view = {
            "schema_version": 3, "native_generation": "00000000-0000-4000-8000-000000000002",
            "mutation_epoch": "0", "selector_transitions": [],
            "engine_instance": {"boot_id": "00000000-0000-4000-8000-000000000001", "pid": 123, "start_ticks": "1000"},
            "effective_config_sha256": channels.digest(self.template),
        }
        self.observations = {
            "schema_version": 3, "binding": self.binding, "engine": copy.deepcopy(engine), "at": 100,
            "paths": {path["id"]: True for path in plan["paths"]},
            "engine_instance_before": copy.deepcopy(self.view["engine_instance"]),
            "engine_instance_after": copy.deepcopy(self.view["engine_instance"]),
            "effective_config_sha256": self.view["effective_config_sha256"],
            "native_generation_before": self.view["native_generation"],
            "native_generation_after": self.view["native_generation"],
            "mutation_epoch_before": "0", "mutation_epoch_after": "0",
        }

    def evaluate(self, view=None, effective=None, now=100):
        with mock.patch.object(execution, "collect", return_value=(
                self.view if view is None else view, self.template if effective is None else effective)) as collect:
            with mock.patch.object(evaluation.time, "time", return_value=now):
                result = evaluation.evaluate(self.config, self.runtime, self.route_id, self.template, self.observations)
        collect.assert_called_once_with()
        return result

    def assert_preview(self, result):
        self.assertIs(result["candidate_only"], True)
        for field in ("applied", "settled", "capability"):
            self.assertIs(result[field], False)
        self.assertEqual(result["deny_outcome"], "not_verified")

    def test_fresh_candidate_and_noop_keep_debt_and_never_dispatch(self):
        self.runtime["pending"] = {"status": "UNKNOWN", "generation": "older"}
        self.runtime["rollback_debt"] = {"required": True}
        before = copy.deepcopy((self.config, self.runtime, self.template, self.observations))
        with ExitStack() as stack:
            for target in ("builtins.open", "pathlib.Path.write_text", "pathlib.Path.write_bytes",
                           "os.replace", "os.remove", "os.unlink", "pathlib.Path.unlink",
                           "subprocess.run", "subprocess.Popen", "urllib.request.urlopen",
                           "http.client.HTTPConnection.request", "http.client.HTTPSConnection.request"):
                stack.enter_context(mock.patch(target, side_effect=AssertionError("forbidden side effect")))
            result = self.evaluate()
        self.assert_preview(result)
        self.assertIs(result["update_required"], False)
        self.assertEqual(result["binding"], self.binding)
        self.assertEqual(result["expected_execution"], {"generation": self.view["native_generation"], "epoch": "0"})
        self.assertEqual(result["candidate"]["runtime"]["pending"], self.runtime["pending"])
        self.assertEqual(result["candidate"]["runtime"]["rollback_debt"], self.runtime["rollback_debt"])
        self.assertEqual(before, (self.config, self.runtime, self.template, self.observations))
        result["candidate"]["config"]["routes"].clear()
        result["candidate"]["runtime"]["pending"].clear()
        result["candidate"]["template"].clear()
        self.assertEqual(before, (self.config, self.runtime, self.template, self.observations))

    def test_all_down_proposes_block_without_claiming_actual_deny(self):
        first = channels.select_paths(self.plan, self.binding, {}, self.observations, 100, self.view)
        second = copy.deepcopy(self.observations)
        second["at"] = 110
        healthy = channels.select_paths(self.plan, self.binding, first, second, 110, self.view)
        self.assertIsNotNone(healthy["selected"])
        self.template, _ = channels.compile_template(self.config, self.route_id, self.plan, self.template,
            healthy, now=110, runtime_view=self.view, effective_config=self.template)
        self.view["effective_config_sha256"] = channels.digest(self.template)
        self.runtime["prepared_routes"][self.route_id] = healthy
        self.observations["effective_config_sha256"] = self.view["effective_config_sha256"]
        self.observations["at"] = 120
        self.observations["paths"] = dict.fromkeys(self.observations["paths"], False)
        result = self.evaluate(now=120)
        self.assert_preview(result)
        self.assertIs(result["update_required"], True)
        rules = result["candidate"]["template"]["routing"]["rules"]
        self.assertEqual(next(rule for rule in rules if rule.get("ruleTag") == "wm-rule-auto-europe")["outboundTag"], xray_template.BLOCK_TAG)

    def test_current_epoch_aba_and_new_generation_refuse_equal_json(self):
        for changes in ({"mutation_epoch": "2"}, {"native_generation": "00000000-0000-4000-8000-000000000003"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.evaluate(view={**self.view, **changes})

    def test_hash_and_process_changes_refuse(self):
        for changes in ({"effective_config_sha256": "f" * 64},
                        {"engine_instance": {**self.view["engine_instance"], "start_ticks": "1001"}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.evaluate(view={**self.view, **changes})

    def test_incomplete_unbound_and_expired_observations_refuse(self):
        original = copy.deepcopy(self.observations)
        invalid = [None, {}, {**original, "binding": "f" * 64}, {**original, "paths": {}},
                   {**original, "schema_version": 2}, {**original, "at": 59}]
        for observation in invalid:
            with self.subTest(observation=observation), self.assertRaises(ValueError):
                self.observations = observation
                self.evaluate()
        self.observations = original

    def test_stored_plan_pinned_engine_and_unpublished_route_required(self):
        original = copy.deepcopy(self.config)
        for change in (lambda r: r.pop("prepared_plan"), lambda r: r["presentation"].update(published=True),
                       lambda r: r["prepared_plan"].update(node_id="other-entry"),
                       lambda r: r["prepared_plan"]["channel"]["engine"].update(version="26.1.1"),
                       lambda r: r["prepared_plan"]["channel"]["engine"].update(sha256="f" * 64)):
            self.config = copy.deepcopy(original)
            change(self.config["routes"][0])
            with mock.patch.object(execution, "collect") as collect, self.assertRaises(ValueError):
                evaluation.evaluate(self.config, self.runtime, self.route_id, self.template, self.observations)
            collect.assert_not_called()
        self.config = original
        self.config["routes"].append(copy.deepcopy(self.config["routes"][0]))
        with self.assertRaises(ValueError):
            self.evaluate()

    def test_unknown_missing_approval_and_unqualified_refuse_without_fallback(self):
        before = copy.deepcopy((self.config, self.runtime, self.template, self.observations))
        for reason in ("EXECUTION_UNQUALIFIED", "EXECUTION_NOT_VERIFIED", "APPROVAL_MISSING"):
            with mock.patch.object(execution, "collect", side_effect=execution.ExecutionUnavailable(reason)) as collect:
                with self.assertRaisesRegex(execution.ExecutionUnavailable, reason):
                    evaluation.evaluate(self.config, self.runtime, self.route_id, self.template, self.observations)
                collect.assert_called_once_with()
            self.assertEqual(before, (self.config, self.runtime, self.template, self.observations))

    def test_consistently_bound_but_unsupported_engine_refuses_before_collection(self):
        original = copy.deepcopy(self.config)
        for changes in ({"version": "26.1.1"}, {"sha256": "f" * 64}):
            self.config = copy.deepcopy(original)
            plan = self.config["routes"][0]["prepared_plan"]
            plan["channel"]["engine"].update(changes)
            for approval in plan["exits"]:
                approval["channel"]["engine"].update(changes)
            channels.bound_plan(self.config, self.route_id, plan)
            with mock.patch.object(execution, "collect") as collect:
                with self.assertRaisesRegex(ValueError, "pinned stock engine"):
                    evaluation.evaluate(self.config, self.runtime, self.route_id, self.template, self.observations)
            collect.assert_not_called()

    def test_clock_is_sampled_after_collect_and_exact_fresh_cas_context_returned(self):
        events = []
        self.view["mutation_epoch"] = "18446744073709551615"
        self.observations["mutation_epoch_before"] = self.observations["mutation_epoch_after"] = self.view["mutation_epoch"]
        with mock.patch.object(execution, "collect", side_effect=lambda: (events.append("collect") or self.view, self.template)):
            with mock.patch.object(evaluation.time, "time", side_effect=lambda: events.append("clock") or 100):
                result = evaluation.evaluate(self.config, self.runtime, self.route_id, self.template, self.observations)
        self.assertEqual(events, ["collect", "clock"])
        self.assertEqual(result["expected_execution"], {"generation": self.view["native_generation"], "epoch": self.view["mutation_epoch"]})
        self.assert_preview(result)

    def test_no_caller_execution_view_or_cache_parameters(self):
        with mock.patch.object(execution, "collect") as collect:
            with self.assertRaises(TypeError):
                evaluation.evaluate(self.config, self.runtime, self.route_id, self.template, self.observations, runtime_view=self.view)
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
