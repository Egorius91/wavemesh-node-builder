import unittest
from unittest import mock
import test_access_runtime as fixtures

runtime = fixtures.runtime


class AbsenceTests(unittest.TestCase):
    def test_failed_lookup_and_unavailable_list_is_not_absence(self):
        panel = mock.Mock()
        panel.call.side_effect = runtime.ProvisionError("transport unavailable")
        with self.assertRaises(runtime.ProvisionError):
            runtime.get_client(panel, "fixture")

    def test_failed_lookup_with_existing_client_in_list_is_not_absence(self):
        panel = mock.Mock()
        panel.call.side_effect = [runtime.ProvisionError("get failed"),
                                  {"success": True, "obj": [{"email": "FIXTURE"}]}]
        with self.assertRaises(runtime.ProvisionError):
            runtime.get_client(panel, "fixture")

    def test_failed_lookup_requires_successful_complete_list(self):
        for listing in [{"success": True, "obj": None}, {"success": False, "obj": []},
                        {"success": True, "obj": {"items": [], "total": 0}},
                        {"success": True, "obj": [{}]}]:
            panel = mock.Mock()
            panel.call.side_effect = [runtime.ProvisionError("missing or failed"), listing]
            with self.assertRaises(runtime.ProvisionError):
                runtime.get_client(panel, "fixture")

    def test_successful_list_can_prove_absence(self):
        panel = mock.Mock()
        panel.call.side_effect = [runtime.ProvisionError("missing"),
                                  {"success": True, "obj": [{"email": "another"}]}]
        self.assertIsNone(runtime.get_client(panel, "fixture"))

    def test_malformed_successful_get_is_not_absence(self):
        panel = mock.Mock()
        panel.call.return_value = {"success": True, "obj": None}
        with self.assertRaises(runtime.ProvisionError):
            runtime.get_client(panel, "fixture")


if __name__ == "__main__":
    unittest.main()
