import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
from panel_isolation import IsolationError, policy, verify_readback, unique_object


class PolicyTest(unittest.TestCase):
    def test_exact_readback_allows_only_handles(self):
        expected = policy(31333, "a" * 64)
        actual = copy.deepcopy(expected)
        for item in actual:
            next(iter(item.values()))["handle"] = 9
        verify_readback({"nftables": [{"metainfo": {}}, *actual]}, expected)
        for index, item in enumerate(expected):
            fields = next(iter(item.values()))
            for key in fields:
                changed = copy.deepcopy(expected)
                del next(iter(changed[index].values()))[key]
                with self.subTest(index=index, key=key), self.assertRaises(IsolationError):
                    verify_readback({"nftables": changed}, expected)
        for altered in (expected[:-1], expected + [expected[-1]], list(reversed(expected))):
            with self.assertRaises(IsolationError):
                verify_readback({"nftables": altered}, expected)

    def test_invalid_port_binding_and_duplicate_json_reject(self):
        for port in (None, True, "31333", 31333.0, -1, 0, 22, 65536):
            with self.subTest(port=port), self.assertRaises(IsolationError):
                policy(port, "a" * 64)
        for binding in (None, {}, "A" * 64, "a" * 63, "../file"):
            with self.assertRaises(IsolationError):
                policy(31333, binding)
        with self.assertRaises(IsolationError):
            json.loads('{"nftables":[],"nftables":[]}', object_pairs_hook=unique_object)


if __name__ == "__main__":
    unittest.main()
