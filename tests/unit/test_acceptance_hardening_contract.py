#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "agent" / "acceptance.py"
UNIT_PATH = ROOT / "agent" / "wavemesh-node-agent.service"
SPEC = importlib.util.spec_from_file_location("wave_node_agent_acceptance_contract", MODULE_PATH)
assert SPEC and SPEC.loader
acceptance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = acceptance
SPEC.loader.exec_module(acceptance)


class AcceptanceHardeningContractTests(unittest.TestCase):
    def test_collector_requirements_exist_in_canonical_unit(self) -> None:
        unit_lines = set(UNIT_PATH.read_text(encoding="utf-8").splitlines())
        self.assertTrue(
            set(acceptance.REQUIRED_HARDENING).issubset(unit_lines),
            "acceptance collector hardening contract drifted from the canonical systemd unit",
        )

    def test_access_runtime_state_directory_is_writable(self) -> None:
        self.assertIn(
            "ReadWritePaths=/etc/wavemesh-agent /etc/wavemesh-node /var/lib/wavemesh-agent",
            acceptance.REQUIRED_HARDENING,
        )


if __name__ == "__main__":
    unittest.main()
