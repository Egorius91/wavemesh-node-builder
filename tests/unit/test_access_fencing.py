from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_access_entitlements as fixtures

runtime = fixtures.runtime


class AccessFencingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccessEntitlementTests()
        self.fixture.setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.fixture.seed(self.root, expiry_ms=1000, limit_ip=1, total_gb=0)
        self.request = self.fixture.request()
        self.config = self.fixture.config()

    def execute(self, request=None):
        with mock.patch.object(runtime, "PanelClient", fixtures.FakePanel):
            return runtime.update_entitlements(request or self.request, self.config, self.root)

    def test_same_command_retry_preserves_identity_without_second_panel_write(self):
        first = self.execute()
        self.assertEqual(self.execute(), first)
        self.assertEqual(len(fixtures.FakePanel.updates), 1)

    def test_same_version_changed_payload_is_rejected_before_panel(self):
        self.execute()
        for field, value in [("quota_bytes", "0"), ("device_limit", 9),
                             ("expires_at", "2027-01-01T00:00:00Z")]:
            with mock.patch.object(runtime, "PanelClient") as panel:
                with self.assertRaises(runtime.ProvisionError):
                    runtime.update_entitlements({**self.request, field: value}, self.config, self.root)
                panel.assert_not_called()

    def test_older_update_provision_and_cleanup_cannot_revert_newer_version(self):
        self.execute()
        self.execute({**self.request, "desired_version": 5, "device_limit": 3})
        with mock.patch.object(runtime, "PanelClient") as panel:
            for function in [runtime.update_entitlements, runtime.provision, runtime.cleanup_previous]:
                with self.assertRaisesRegex(runtime.ProvisionError, "stale"):
                    function(self.request, self.config, self.root)
            panel.assert_not_called()
        self.assertEqual(len(fixtures.FakePanel.updates), 2)

    def test_failure_after_durable_fence_allows_only_identical_retry(self):
        with mock.patch.object(runtime, "PanelClient", side_effect=runtime.ProvisionError("unavailable")):
            with self.assertRaises(runtime.ProvisionError):
                runtime.update_entitlements(self.request, self.config, self.root)
        with self.assertRaises(runtime.ProvisionError):
            self.execute({**self.request, "device_limit": 3})
        self.execute()
        self.assertEqual(len(fixtures.FakePanel.updates), 1)

    def test_legacy_same_version_requires_reconciliation_but_newer_version_works(self):
        state = {**self.fixture.previous_state(), "desired_version": 4}
        runtime.atomic_json(self.root / "access_12345678.4.json", state)
        with mock.patch.object(runtime, "PanelClient") as panel:
            with self.assertRaisesRegex(runtime.ProvisionError, "cannot be verified"):
                runtime.update_entitlements(self.request, self.config, self.root)
            panel.assert_not_called()
        self.execute({**self.request, "desired_version": 5})

    def test_cleanup_does_not_delete_identity_shared_with_older_entitlement_version(self):
        self.execute()
        with mock.patch.object(runtime, "PanelClient", fixtures.FakePanel):
            self.assertEqual(runtime.cleanup_previous(self.request, self.config, self.root), 0)
        self.assertIn(self.fixture.previous_state()["panel_email"], fixtures.FakePanel.clients)

    def test_concurrent_process_is_rejected_and_process_death_releases_lock(self):
        code = """
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('worker_runtime', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with module.access_lock(pathlib.Path(sys.argv[2]), 'access_12345678'):
    print('LOCKED', flush=True)
    sys.stdin.read()
"""
        child = subprocess.Popen([sys.executable, "-c", code, runtime.__file__, str(self.root)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "LOCKED")
            with mock.patch.object(runtime, "PanelClient") as panel:
                with self.assertRaises(OSError):
                    runtime.update_entitlements(self.request, self.config, self.root)
                panel.assert_not_called()
        finally:
            child.kill()
            child.communicate(timeout=10)
        self.execute()
        self.assertEqual(len(fixtures.FakePanel.updates), 1)


if __name__ == "__main__":
    unittest.main()
