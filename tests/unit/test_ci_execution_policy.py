"""Offline guards for trigger deduplication; no runtime test selection."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
ALWAYS_CLEANUP = r'''      - name: Independently reconcile central SQL fixture cleanup
        if: always()
        timeout-minutes: 1
        run: |
          sudo python3 -I -B tests/native/test_xui_owner_central_postgres.py --finalize \
            --state "$RUNNER_TEMP/wm-central-sql-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.json"
'''


def required_execution(workflow):
    jobs = workflow.split('\njobs:\n', 1)[1]
    # This one exact unconditional cleanup is additive: it cannot replace,
    # condition or soften the primary native proof or any required job/step.
    if jobs.count(ALWAYS_CLEANUP) != 1:
        raise AssertionError('CI_CLEANUP_MUST_BE_EXACT_AND_SINGLE')
    checked = jobs.replace(ALWAYS_CLEANUP, ALWAYS_CLEANUP.replace('        if: always()\n', ''), 1)
    if re.search(r'''(?<![A-Za-z0-9_])(?:if|'if'|"if")\s*:''', checked):
        raise AssertionError('CI_REQUIRED_JOB_OR_STEP_CONDITIONAL')
    if 'continue-on-error' in jobs:
        raise AssertionError('CI_CONTINUE_ON_ERROR_FORBIDDEN')
    for required in ('      - name: Native synthetic central PostgreSQL SQL and catalog proof\n',
                     'test_xui_owner_central_postgres.py --require-native'):
        if jobs.count(required) != 1:
            raise AssertionError('CI_PRIMARY_NATIVE_PROOF_MUST_REMAIN_REQUIRED')
    return jobs


class CIExecutionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / '.github/workflows/tests.yml').read_text()

    def test_pr_and_main_remain_but_feature_push_does_not_duplicate(self):
        triggers = self.workflow.split('on:\n', 1)[1].split('\n# Only superseded', 1)[0]
        self.assertEqual(triggers.strip(), 'push:\n    branches: [main]\n  pull_request:')

    def test_only_same_pr_can_cancel_obsolete_work(self):
        self.assertIn('group: builder-tests-${{ github.event_name }}-${{ github.event.pull_request.number || github.ref }}', self.workflow)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", self.workflow)

    def test_required_runtime_and_security_coverage_is_not_skipped(self):
        jobs = required_execution(self.workflow)
        for command in ('WAVEMESH_REQUIRE_NGINX_TESTS: "1"', 'for test in tests/unit/test_*.py',
                        'python3 tests/e2e/test_multi_exit.py', 'bash tests/unit/test_transaction.sh',
                        'bash tests/integration/test_xui_api.sh', 'bash tests/smoke/node_agent_unit_hardening.sh',
                        'bash tests/smoke/node_agent_installer.sh', 'bash tests/smoke/node_agent_rollback_latest.sh'):
            self.assertIn(command, jobs)

    def test_added_job_or_required_step_condition_still_refused(self):
        for old, new in (
            ('  test:\n', '  test:\n    if: false\n'),
            ('      - name: Python unit and E2E tests\n', '      - name: Python unit and E2E tests\n        if: false\n'),
            ('      - name: Native synthetic central PostgreSQL SQL and catalog proof\n',
             '      - name: Native synthetic central PostgreSQL SQL and catalog proof\n        "if": false\n')):
            with self.subTest(old=old), self.assertRaisesRegex(AssertionError,'CI_REQUIRED_JOB_OR_STEP_CONDITIONAL'):
                required_execution(self.workflow.replace(old,new,1))

    def test_continue_on_error_job_step_or_cleanup_still_refused(self):
        for anchor, indent in (('  test:\n','    '),('      - name: Python unit and E2E tests\n','        ')):
            with self.subTest(anchor=anchor),self.assertRaisesRegex(AssertionError,'CI_CONTINUE_ON_ERROR_FORBIDDEN'):
                required_execution(self.workflow.replace(anchor,anchor+indent+'continue-on-error: true\n',1))
        with self.assertRaises(AssertionError):
            required_execution(self.workflow.replace('        if: always()\n','        if: always()\n        continue-on-error: true\n',1))

    def test_conditional_renamed_duplicate_or_broadened_cleanup_refused(self):
        for changed in (self.workflow.replace('if: always()','if: cancelled()',1),
                        self.workflow.replace('Independently reconcile central SQL fixture cleanup','Other cleanup',1),
                        self.workflow.replace(ALWAYS_CLEANUP,ALWAYS_CLEANUP+ALWAYS_CLEANUP,1),
                        self.workflow.replace('--finalize \\','--require-native \\',1)):
            with self.subTest(changed=changed==self.workflow),self.assertRaises(AssertionError):
                required_execution(changed)

    def test_primary_proof_cannot_be_removed_while_cleanup_remains(self):
        with self.assertRaisesRegex(AssertionError,'CI_PRIMARY_NATIVE_PROOF_MUST_REMAIN_REQUIRED'):
            required_execution(self.workflow.replace('test_xui_owner_central_postgres.py --require-native',
                                                     'echo primary-proof-skipped',1))


if __name__ == '__main__':
    unittest.main()
