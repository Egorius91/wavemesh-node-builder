#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "agent" / "wavemesh_node_agent.py"
SPEC = importlib.util.spec_from_file_location("wavemesh_node_agent", MODULE_PATH)
assert SPEC and SPEC.loader
agent = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = agent
SPEC.loader.exec_module(agent)


class NodeAgentTests(unittest.TestCase):
    def test_build_observation_state_is_redacted(self) -> None:
        route_health = {
            "node_status": "healthy",
            "observed_at": "2026-07-25T10:39:35Z",
            "routes": [
                {
                    "route_id": "route-de-fra-1",
                    "display_name": "RU -> Germany",
                    "exit_id": "de-fra-1",
                    "enabled": True,
                    "status": "healthy",
                    "latency_ms": 828,
                    "outbound": "wm-exit-de-fra-1",
                    "last_check": "2026-07-25T10:39:35Z",
                    "uuid": "must-not-leak",
                }
            ],
        }
        auto_health = {
            "auto_routes": [
                {
                    "id": "route-auto-auto-europe",
                    "display_name": "Auto Europe",
                    "status": "healthy",
                    "enabled": True,
                    "published": True,
                    "strategy": "leastPing",
                    "selectors": ["wm-exit-de-fra-1"],
                    "healthy_exits": 1,
                    "total_exits": 1,
                }
            ]
        }

        state = agent.build_observation_state(route_health, auto_health)
        agent.assert_redacted(state)
        encoded = json.dumps(state)
        self.assertNotIn("uuid", encoded.lower())
        self.assertNotIn("outbound", encoded.lower())
        self.assertNotIn("selectors", encoded.lower())
        self.assertNotIn("display_name", encoded.lower())
        self.assertEqual(state["healthy_exits"], 1)
        self.assertEqual(state["total_exits"], 1)

    def test_env_rotation_update_is_atomic_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "agent.env"
            initial_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
            values = {
                "WAVEMESH_API_BASE": "https://example.invalid/api",
                "WAVEMESH_NODE_ID": "node-12345678",
                "WAVEMESH_TENANT_ID": "tenant-12345678",
                "WAVEMESH_AGENT_TOKEN": agent.generate_token(),
                "WAVEMESH_AGENT_TOKEN_EXPIRES_AT": agent.format_timestamp(initial_expiry),
                "WAVEMESH_AGENT_MODE": "observe-only",
            }
            agent.write_env_file(env_path, values)
            config = agent.AgentConfig.load(env_path)
            replacement = agent.generate_token()
            replacement_expiry = datetime.now(timezone.utc) + timedelta(hours=24)

            config.save_rotated_token(replacement, replacement_expiry)

            stored = agent.read_env_file(env_path)
            self.assertEqual(stored["WAVEMESH_AGENT_TOKEN"], replacement)
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.agent_token, replacement)
            self.assertTrue(agent.valid_token(replacement))
            self.assertEqual(len(agent.token_hash(replacement)), 64)

    def test_forbidden_observation_key_is_rejected(self) -> None:
        with self.assertRaises(agent.AgentError):
            agent.assert_redacted({"api_token": "not-allowed"})


if __name__ == "__main__":
    unittest.main()
