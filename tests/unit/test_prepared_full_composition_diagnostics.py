"""Source-only phase/error privacy proof; never runs the native composition."""
import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "tests/native/prepared_full_composition.py"
SPEC = importlib.util.spec_from_file_location("pr4_diagnostic_fixture", ENTRYPOINT)
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)
POISON = "POISON_secret_bearer_DB_config_request_https_user_password"


class UnprintableError(RuntimeError):
    def __str__(self):
        raise AssertionError("exception formatting is forbidden")

    def __repr__(self):
        raise AssertionError("exception repr is forbidden")

    @property
    def status(self):
        raise AssertionError("unstructured HTTP status is forbidden")


class DiagnosticsTests(unittest.TestCase):
    def assert_clean(self, result):
        encoded = json.dumps(result, allow_nan=False)
        self.assertNotIn(POISON, encoded)
        self.assertNotIn("httpStatus", result)

    def test_fault_families_never_format_or_export_poison_arguments(self):
        errors = [RuntimeError(POISON), ValueError(POISON), AssertionError(POISON),
                  OSError(POISON), KeyError(POISON), Exception(POISON), UnprintableError(POISON),
                  FIXTURE.execution.ExecutionUnavailable(POISON), FIXTURE.owner.OwnerUnavailable(POISON),
                  FIXTURE.intent_writer.IntentUnavailable(POISON), FIXTURE.completion.CompletionUnavailable(POISON),
                  FIXTURE.sqlite3.OperationalError(POISON),
                  FIXTURE.subprocess.CalledProcessError(1, [POISON], output=POISON, stderr=POISON),
                  FIXTURE.subprocess.TimeoutExpired([POISON], 1, output=POISON, stderr=POISON)]
        for error in errors:
            with self.subTest(kind=type(error).__name__):
                self.assert_clean(FIXTURE.diagnostic_error(error))

    def test_secret_in_exception_class_name_is_not_exported(self):
        error = type(POISON, (Exception,), {})(POISON)
        self.assertEqual(FIXTURE.diagnostic_error(error),
                         {"failureType": "UnclassifiedError", "errorCode": "UNCLASSIFIED_ERROR"})

    def test_collector_accepts_only_one_exact_source_enum(self):
        good = FIXTURE.execution.ExecutionUnavailable("SNAPSHOT_TIMEOUT")
        self.assertEqual(FIXTURE.diagnostic_error(good)["errorCode"], "SNAPSHOT_TIMEOUT")
        for value in ("SNAPSHOT_TIMEOUT:" + POISON, " " + "SNAPSHOT_TIMEOUT", {"code": POISON}):
            result = FIXTURE.diagnostic_error(FIXTURE.execution.ExecutionUnavailable(value))
            self.assertEqual(result["errorCode"], "EXECUTION_UNAVAILABLE")
            self.assert_clean(result)

    def test_poisoned_or_wrong_family_reason_cannot_become_output(self):
        self.assert_clean(FIXTURE.diagnostic_error(RuntimeError(POISON), POISON))
        result = FIXTURE.diagnostic_error(ValueError(POISON), "FD9_OCCUPIED")
        self.assertEqual(result["errorCode"], "INVALID_VALUE")
        self.assertEqual(FIXTURE.diagnostic_error(RuntimeError(POISON), "FD9_OCCUPIED")["errorCode"], "FD9_OCCUPIED")

    def test_failure_phase_and_duration_are_frozen_before_finalization(self):
        clock = SimpleNamespace(now=10.0)
        trace = FIXTURE.PhaseDiagnostics(lambda: clock.now)
        clock.now = 12.0
        trace.enter("SNAPSHOT_WAIT")
        clock.now = 72.0
        trace.reason = "SNAPSHOT_WAIT_EXPIRED"
        failure = trace.failure(RuntimeError(POISON))
        self.assertEqual(failure["failedPhase"], "SNAPSHOT_WAIT")
        self.assertEqual(failure["phaseSeconds"], 60.0)
        self.assertEqual(failure["phaseDurations"], {"PRECHECK": 2.0, "SNAPSHOT_WAIT": 60.0})
        clock.now = 90.0
        self.assertEqual(trace.stop()["phaseSeconds"], 60.0)
        self.assert_clean(failure)

    def test_invalid_clock_values_and_clock_exception_are_not_serialized(self):
        for invalid in (float("nan"), float("inf"), -1.0, 1801.0, POISON):
            clock = SimpleNamespace(now=0.0)
            trace = FIXTURE.PhaseDiagnostics(lambda: clock.now)
            clock.now = invalid
            result = trace.failure(UnprintableError(POISON))
            self.assertIsNone(result["phaseSeconds"])
            self.assert_clean(result)
        def broken_clock():
            raise UnprintableError(POISON)
        self.assert_clean(FIXTURE.PhaseDiagnostics(broken_clock).failure(UnprintableError(POISON)))

    def test_unapproved_phase_is_refused_without_echoing_it(self):
        trace = FIXTURE.PhaseDiagnostics(lambda: 0.0)
        with self.assertRaisesRegex(ValueError, "^UNSUPPORTED_DIAGNOSTIC_PHASE$"):
            trace.enter(POISON)

    def test_actual_wait_guard_distinguishes_original_exit_and_deadline(self):
        tree = ast.parse(ENTRYPOINT.read_bytes())
        run = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
        loop = next(node for node in ast.walk(run) if isinstance(node, ast.While)
                    and ast.unparse(node.test) == "not execution.SNAPSHOT.exists()")
        for status, expected in ((0, "PRODUCER_EARLY_EXIT"), (None, "SNAPSHOT_WAIT_EXPIRED")):
            trace = FIXTURE.PhaseDiagnostics(lambda: 0.0)
            trace.enter("SNAPSHOT_WAIT")
            namespace = {"execution": SimpleNamespace(SNAPSHOT=SimpleNamespace(exists=lambda: False)),
                         "process": SimpleNamespace(poll=lambda: status), "time": SimpleNamespace(monotonic=lambda: 10.0),
                         "deadline": 10.0, "diagnostics": trace}
            with self.assertRaisesRegex(RuntimeError, "^original Producer not ready$") as caught:
                exec(compile(ast.Module(body=[loop], type_ignores=[]), str(ENTRYPOINT), "exec"), namespace)
            self.assertEqual(trace.failure(caught.exception)["errorCode"], expected)


if __name__ == "__main__":
    unittest.main()
