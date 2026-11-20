"""Inspect the fixture AST only; never execute its Linux run() entrypoint."""
import ast
from pathlib import Path
import unittest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "tests/native/prepared_full_composition.py"


class FullCompositionLogOwnership(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(ENTRYPOINT.read_text(encoding="utf-8"))
        self.run = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
        self.assignments = {
            target.id: node for node in ast.walk(self.run) if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
        }

    def test_both_producer_launches_bind_existing_owned_private_log_folder(self):
        env = self.assignments["env"].value
        fields = {key.value: value for key, value in zip(env.keys, env.values)}
        self.assertIn("XUI_LOG_FOLDER", fields)
        folder = ast.literal_eval(fields["XUI_LOG_FOLDER"])
        private_log = self.assignments["private_log"].value
        self.assertEqual(private_log.func.id, "Path")
        self.assertEqual(folder, ast.literal_eval(private_log.args[0]))
        self.assertEqual(folder, "/var/log/wavemesh-node")
        owned = [node for node in ast.walk(self.run) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == "owned" and node.func.attr == "append"
                 and len(node.args) == 1 and isinstance(node.args[0], ast.Name)
                 and node.args[0].id == "private_log"]
        self.assertEqual(len(owned), 1)
        launches = [node for node in ast.walk(self.run) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess" and node.func.attr in ("run", "Popen")
                    and any(kw.arg == "env" and isinstance(kw.value, ast.Name) and kw.value.id == "env"
                            for kw in node.keywords)]
        self.assertEqual(len(launches), 2)  # migrate and the same original daemon
        self.assertLess(owned[0].lineno, min(node.lineno for node in launches))

    def test_console_capture_remains_separate_from_panel_and_core_file_logs(self):
        log = self.assignments["log"].value
        self.assertIsInstance(log, ast.Call)
        self.assertEqual(log.func.attr, "open")
        self.assertEqual(log.func.value.right.value, "producer.log")
        self.assertEqual(log.args[0].value, "wb")
        self.assertNotIn("/var/log/x-ui", ast.unparse(self.run))


if __name__ == "__main__":
    unittest.main()
