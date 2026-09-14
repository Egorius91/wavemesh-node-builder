import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
from panel_isolation import IsolationError, policy, verify_readback, unique_object


class PolicyTest(unittest.TestCase):
    def test_kernel_order_and_security_semantic_drift(self):
        expected = policy(31333, "a" * 64)
        self.assertEqual([next(iter(item)) for item in expected],
                         ["table", "chain", "chain", "rule", "rule"])
        changes = [
            (0, "comment", "wm-install:" + "b" * 64),
            (1, "hook", "forward"), (1, "prio", 0), (1, "policy", "drop"),
            (2, "hook", "input"), (3, "chain", "output"),
        ]
        for index, field, value in changes:
            actual = copy.deepcopy(expected)
            next(iter(actual[index].values()))[field] = value
            with self.subTest(index=index, field=field), self.assertRaises(IsolationError):
                verify_readback({"nftables": actual}, expected)
        for index in (3, 4):
            for expr_index in range(len(expected[index]["rule"]["expr"])):
                actual = copy.deepcopy(expected)
                actual[index]["rule"]["expr"][expr_index] = {"accept": None}
                with self.subTest(rule=index, expression=expr_index), self.assertRaises(IsolationError):
                    verify_readback({"nftables": actual}, expected)

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
