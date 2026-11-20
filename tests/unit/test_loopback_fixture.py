import json
from pathlib import Path
import unittest
from tests.unit.loopback_fixture import builder_loopback_cases


class LoopbackFixture(unittest.TestCase):
    def test_committed_panel_payloads_are_the_actual_builder_output(self):
        path=Path(__file__).resolve().parents[2]/'integrations/3xui-prepared-execution/fixtures/builder-loopback-inbounds.json'
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),builder_loopback_cases())


if __name__=='__main__':unittest.main()
