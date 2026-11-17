"""Legacy health refresh must preserve the prepared controller's state."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("namespace_runtime", ROOT / "scripts/lib/runtime_state.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeNamespaceOwnership(unittest.TestCase):
    def test_health_rebuilds_own_fields_and_preserves_other_owners_without_aliasing(self):
        previous = {
            "observed_at": "old", "node_status": "unhealthy", "xui": {"service": "inactive"},
            "routes": {"removed": {"status": "healthy"}}, "exits": {"removed": {}},
            "prepared_routes": {"route-one": {"mutation_epoch": "7", "sample_at": 100,
                "engine_instance": {"pid": 123}, "selected": None, "deny_outcome": "unknown"}},
            "other_owner": {"revision": 2, "values": [1, False]},
        }
        before = copy.deepcopy(previous)
        with tempfile.TemporaryDirectory() as temporary:
            observed = runtime.evaluate({"routes": [], "exits": []}, previous,
                                       {"obj": []}, {}, "", Path(temporary),
                                       {"control": {"service": "active"}})
        self.assertEqual(observed["prepared_routes"], before["prepared_routes"])
        self.assertEqual(observed["other_owner"], before["other_owner"])
        self.assertEqual(observed["routes"], {})
        self.assertEqual(observed["exits"], {})
        self.assertEqual(observed["xui"], {"service": "active"})
        self.assertEqual(observed["node_status"], "healthy")
        self.assertNotEqual(observed["observed_at"], "old")
        self.assertEqual(previous, before)
        observed["prepared_routes"]["route-one"]["mutation_epoch"] = "8"
        observed["other_owner"]["values"].append("changed")
        self.assertEqual(previous, before)


if __name__ == "__main__":
    unittest.main()
