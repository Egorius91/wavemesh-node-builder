"""Selection/import/admission contracts only; these do not run the native49."""
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import prepared_native_units as entrypoint
from test_ci_execution_policy import required_execution

ROOT = Path(__file__).resolve().parents[2]
NATIVE_COMMAND = "sudo -n unshare --net -- sh -ec 'ip link set dev lo up; exec \"$@\"' sh \"$(command -v python3)\" -I -B tests/prepared_native_units.py"
EXPECTED = {
    "test_prepared_owning_caller": 32,
    "test_prepared_owning_transaction": 6,
    "test_prepared_transaction": 6,
    "test_agent_prepared_origin": 5,
}


def mandatory_partition(workflow):
    jobs = required_execution(workflow)
    if jobs.count(NATIVE_COMMAND) != 1 or not re.search(r'^        run: ' + re.escape(NATIVE_COMMAND) + r'$', jobs, re.M):
        raise AssertionError("PREPARED_NATIVE_COMMAND_MUST_BE_REQUIRED_AND_SINGLE")
    selection = re.search(r'case "\$test" in\n(.*?)\n\s*esac', jobs, re.S)
    if selection is None:
        raise AssertionError("PREPARED_NATIVE_PARTITION_MISSING")
    whole = re.search(r'^\s*(tests/unit/[^\n)]+)\) continue ;;$', selection[1], re.M)
    moved = set(re.findall(r'tests/unit/(test_\w+)\.py', whole[1])) if whole else set()
    if moved != set(EXPECTED) | {'test_prepared_panel_peer_native'} or selection[1].count('continue ;;') != 2:
        raise AssertionError("PREPARED_NATIVE_PARTITION_MUST_MATCH_MANDATORY_SUITE")
    ordinary_command = 'PYTHONPATH="$PWD" python3 tests/prepared_native_units.py --ordinary-completion'
    if jobs.count(ordinary_command) != 1 or 'tests/unit/test_prepared_completion.py)\n' not in selection[1]:
        raise AssertionError("COMPLETION_ORDINARY_ROLE_MUST_REMAIN_REQUIRED")
    if 'PYTHONPATH="$PWD" python3 "$test"' not in jobs:
        raise AssertionError("ORDINARY_SCRIPT_AND_PACKAGE_IMPORT_COVERAGE_MISSING")
    return moved


class PreparedNativeEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / '.github/workflows/tests.yml').read_text()

    def test_real_repository_collection_finds_exact_49_without_running_them(self):
        self.assertEqual(entrypoint.SUITES, EXPECTED)
        for name, count in EXPECTED.items():
            with self.subTest(name=name):
                self.assertEqual(entrypoint.load_suite(name, count).countTestCases(), count)

    def test_changed_collection_cannot_silently_reduce_native_coverage(self):
        with self.assertRaisesRegex(RuntimeError, 'COVERAGE_CHANGED'):
            entrypoint.load_suite('test_prepared_owning_caller', 31)

    def test_foreign_preloaded_test_package_is_refused(self):
        with mock.patch.object(entrypoint.importlib, 'import_module',
                               return_value=SimpleNamespace(__file__='/foreign/tests/__init__.py')):
            with self.assertRaisesRegex(RuntimeError, 'FOREIGN_TEST_PACKAGE'):
                entrypoint.load_suite('test_prepared_owning_caller', 32)

    def test_admission_decision_refuses_each_missing_native_requirement(self):
        valid = ['linux', 0, '/usr/bin/bash', (3, 12), ['lo']]
        for position, value in [(0, 'win32'), (1, 1001), (2, None), (3, (3, 11)),
                                (4, ['lo', 'eth0']), (4, [])]:
            supplied = valid.copy()
            supplied[position] = value
            with self.subTest(position=position, value=value):
                self.assertIsNotNone(entrypoint.admission_error(*supplied))
        # Pure decision inputs only: this does not claim Linux/root was executed.
        self.assertIsNone(entrypoint.admission_error(*valid))

    def test_rejected_admission_precedes_any_fixture_or_test_import(self):
        with (mock.patch.object(entrypoint, 'require_native', side_effect=RuntimeError('ADMISSION_REFUSED')),
              mock.patch.object(entrypoint.tempfile, 'TemporaryDirectory') as temporary,
              mock.patch.object(entrypoint, 'load_suite') as load):
            with self.assertRaisesRegex(RuntimeError, 'ADMISSION_REFUSED'):
                entrypoint.main([])
            temporary.assert_not_called()
            load.assert_not_called()

    def test_skips_failures_and_incomplete_counts_cannot_pass(self):
        for count, success, skips in [(48, True, []), (49, False, []), (49, True, [('case', 'skip')])]:
            result = SimpleNamespace(testsRun=count, wasSuccessful=lambda: success, skipped=skips)
            with self.subTest(count=count, success=success, skips=skips):
                self.assertFalse(entrypoint.completed_without_skips(result, 49))
        good = SimpleNamespace(testsRun=49, wasSuccessful=lambda: True, skipped=[])
        self.assertTrue(entrypoint.completed_without_skips(good, 49))

    def test_all_existing_unit_files_are_partitioned_without_duplicate_native_runs(self):
        moved = mandatory_partition(self.workflow)
        files = {p.stem for p in (ROOT / 'tests/unit').glob('test_*.py')}
        self.assertTrue(moved <= files)
        ordinary = files - moved - {'test_prepared_completion'}
        self.assertFalse(ordinary & moved)
        self.assertEqual(ordinary | moved | {'test_prepared_completion'}, files)
        self.assertIn('test_prepared_native_entrypoint', ordinary)

    def test_root_namespace_or_entrypoint_removal_is_rejected(self):
        for replacement in ['python3 tests/prepared_native_units.py',
                            NATIVE_COMMAND.replace('sudo -n ', ''),
                            NATIVE_COMMAND.replace('unshare --net -- ', ''),
                            NATIVE_COMMAND.replace(' -I -B ', ' ')]:
            with self.subTest(replacement=replacement), self.assertRaises(AssertionError):
                mandatory_partition(self.workflow.replace(NATIVE_COMMAND, replacement, 1))

    def test_missing_extra_or_duplicate_native_selection_is_rejected(self):
        for changed in [self.workflow.replace('tests/unit/test_prepared_transaction.py|', '', 1),
                        self.workflow.replace('tests/unit/test_prepared_transaction.py|',
                            'tests/unit/test_prepared_transaction.py|tests/unit/test_prepared_intent.py|', 1),
                        self.workflow.replace(NATIVE_COMMAND, NATIVE_COMMAND + '\n        run: ' + NATIVE_COMMAND, 1)]:
            with self.assertRaises(AssertionError):
                mandatory_partition(changed)

    def test_conditional_or_softened_native_step_is_rejected(self):
        anchor = '      - name: Mandatory prepared owning synthetic units (49 plus 6)\n'
        self.assertEqual(self.workflow.count(anchor), 1)
        for addition in ['        if: false\n', '        continue-on-error: true\n']:
            with self.assertRaises(AssertionError):
                mandatory_partition(self.workflow.replace(anchor, anchor + addition, 1))

    def test_complete_role_union_is_55_root_plus_13_ordinary_without_duplicate_cases(self):
        native = list(entrypoint.native_suites())
        self.assertEqual([count for _, _, count in native], [32, 6, 6, 5, 4, 2])
        root_ids = [case.id() for _, suite, _ in native for case in entrypoint.cases(suite)]
        ordinary, completion_root = entrypoint.completion_roles()
        ordinary_ids = [case.id() for case in entrypoint.cases(ordinary)]
        all_completion = {case.id() for case in entrypoint.cases(entrypoint.load_suite('test_prepared_completion', 15))}
        root_completion_ids = {case.id() for case in entrypoint.cases(completion_root)}
        self.assertEqual(len(root_ids), 55)
        self.assertEqual(len(set(root_ids)), 55)
        self.assertEqual(len(ordinary_ids), 13)
        self.assertFalse(set(root_ids) & set(ordinary_ids))
        self.assertEqual(set(ordinary_ids) | root_completion_ids, all_completion)
        self.assertEqual(len(set(root_ids) | set(ordinary_ids)), 68)

    def test_ordinary_completion_excludes_only_two_expected_role_cases_without_skips(self):
        ordinary, _ = entrypoint.completion_roles()
        for case in entrypoint.cases(ordinary):
            self.assertFalse(getattr(case, '__unittest_skip__', False))
            self.assertFalse(getattr(type(case), '__unittest_skip__', False))
        # This inspects collection only: the13 fixture methods are not executed.

    def test_missing_root_completion_method_cannot_be_called_complete_coverage(self):
        with mock.patch.object(entrypoint, 'ROOT_COMPLETION_METHODS',
                               frozenset(list(entrypoint.ROOT_COMPLETION_METHODS)[:1])):
            with self.assertRaisesRegex(RuntimeError, 'ROLE_COVERAGE_CHANGED'):
                entrypoint.completion_roles()

    def test_explicit_ordinary_role_does_not_enter_root_runner(self):
        with (mock.patch.object(entrypoint, 'ordinary_completion', return_value=0) as ordinary,
              mock.patch.object(entrypoint, 'require_native') as native):
            self.assertEqual(entrypoint.main(['--ordinary-completion']), 0)
            ordinary.assert_called_once_with()
            native.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'UNKNOWN_ROLE'):
            entrypoint.main(['--arbitrary-test-filter'])

    def test_completion_role_dispatch_cannot_be_removed_or_duplicated(self):
        command = 'PYTHONPATH="$PWD" python3 tests/prepared_native_units.py --ordinary-completion'
        for replacement in ['', command + '\n                ' + command]:
            with self.assertRaises(AssertionError):
                mandatory_partition(self.workflow.replace(command, replacement, 1))

    def test_shell_soft_failure_cannot_turn_required_native_execution_green(self):
        with self.assertRaises(AssertionError):
            mandatory_partition(self.workflow.replace(NATIVE_COMMAND, NATIVE_COMMAND + ' || true', 1))

    def test_loopback_bringup_is_required_for_existing_peer_tcp_fixtures(self):
        self.assertIn('sudo apt-get install -y nginx unzip iproute2', self.workflow)
        with self.assertRaises(AssertionError):
            mandatory_partition(self.workflow.replace('ip link set dev lo up; ', '', 1))

    def test_existing_owner_transitive_cases_are_preserved_in_ordinary_role(self):
        owner = {case.id() for case in entrypoint.cases(entrypoint.load_suite('test_prepared_owner', 18))}
        ordinary, native = entrypoint.completion_roles()
        ordinary_ids = {case.id() for case in entrypoint.cases(ordinary)}
        native_ids = {case.id() for case in entrypoint.cases(native)}
        self.assertEqual(len(owner & ordinary_ids), 8)
        self.assertFalse(owner & native_ids)
        # These8 ordinary duplicates predate this proposal; retain script semantics.


if __name__ == '__main__':
    unittest.main()
