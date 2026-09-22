"""Offline guards for trigger deduplication; no runtime test selection."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


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
        jobs = self.workflow.split('\njobs:\n', 1)[1]
        self.assertNotIn('if:', jobs)
        self.assertNotIn('continue-on-error', jobs)
        for command in ('WAVEMESH_REQUIRE_NGINX_TESTS: "1"', 'for test in tests/unit/test_*.py',
                        'python3 tests/e2e/test_multi_exit.py', 'bash tests/unit/test_transaction.sh',
                        'bash tests/integration/test_xui_api.sh', 'bash tests/smoke/node_agent_unit_hardening.sh',
                        'bash tests/smoke/node_agent_installer.sh', 'bash tests/smoke/node_agent_rollback_latest.sh'):
            self.assertIn(command, jobs)


if __name__ == '__main__':
    unittest.main()
