#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "agent" / "node_agent.py"
SPEC = importlib.util.spec_from_file_location("wave_node_agent", MODULE_PATH)
assert SPEC and SPEC.loader
agent = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = agent
SPEC.loader.exec_module(agent)


def assert_posix_mode(case: unittest.TestCase, path: Path, expected: int) -> None:
    if os.name != "nt":
        case.assertEqual(path.stat().st_mode & 0o777, expected)


class NodeAgentTests(unittest.TestCase):
    def test_access_command_validation_is_allowlisted_and_strict(self) -> None:
        payload = {
            "access_id": "access_12345678",
            "desired_version": 1,
            "enabled": True,
            "expires_at": "2026-08-01T00:00:00Z",
            "device_limit": 1,
            "quota_bytes": "1073741824",
        }
        command = {
            "command_id": "command_12345678",
            "schema_version": 1,
            "target_node_id": "node-12345678",
            "type": "access.provision",
            "attempt": 1,
            "payload": payload,
        }

        command_id, attempt, command_type, validated = agent.validate_access_command(
            command, "node-12345678"
        )
        self.assertEqual(command_id, "command_12345678")
        self.assertEqual(attempt, 1)
        self.assertEqual(command_type, "access.provision")
        self.assertEqual(validated, payload)
        replacement = agent.validate_access_command(
            {**command, "type": "access.replace_credential"}, "node-12345678"
        )
        self.assertEqual(replacement[2], "access.replace_credential")
        with self.assertRaises(agent.AgentError):
            agent.validate_access_command(
                {**command, "type": "shell.execute"}, "node-12345678"
            )
        with self.assertRaises(agent.AgentError):
            agent.validate_access_command(
                {**command, "payload": {**payload, "shell": "id"}},
                "node-12345678",
            )

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
            assert_posix_mode(self, env_path, 0o600)
            self.assertEqual(config.agent_token, replacement)
            self.assertTrue(agent.valid_token(replacement))
            self.assertEqual(len(agent.token_hash(replacement)), 64)

    def test_pending_rotation_token_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "agent.env"
            agent.write_env_file(
                env_path,
                {
                    "WAVEMESH_API_BASE": "https://example.invalid/api",
                    "WAVEMESH_NODE_ID": "node-12345678",
                    "WAVEMESH_TENANT_ID": "tenant-12345678",
                    "WAVEMESH_AGENT_TOKEN": agent.generate_token(),
                    "WAVEMESH_AGENT_TOKEN_EXPIRES_AT": agent.format_timestamp(
                        datetime.now(timezone.utc) + timedelta(hours=1)
                    ),
                    "WAVEMESH_AGENT_MODE": "observe-only",
                },
            )
            first = agent.NodeAgent(agent.AgentConfig.load(env_path))
            pending = first.load_or_create_pending_replacement()
            second = agent.NodeAgent(agent.AgentConfig.load(env_path))
            self.assertEqual(second.load_or_create_pending_replacement(), pending)
            assert_posix_mode(self, second.config.pending_rotation_path, 0o600)
            second.clear_pending_replacement()
            self.assertFalse(second.config.pending_rotation_path.exists())

    def test_rotation_scaling_defaults_preserve_existing_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "agent.env"
            expiry = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
            token = agent.generate_token()
            agent.write_env_file(
                env_path,
                {
                    "WAVEMESH_API_BASE": "https://example.invalid/api",
                    "WAVEMESH_NODE_ID": "node-12345678",
                    "WAVEMESH_TENANT_ID": "tenant-12345678",
                    "WAVEMESH_AGENT_TOKEN": token,
                    "WAVEMESH_AGENT_TOKEN_EXPIRES_AT": agent.format_timestamp(expiry),
                    "WAVEMESH_AGENT_MODE": "observe-only",
                },
            )

            config = agent.AgentConfig.load(env_path)
            instance = agent.NodeAgent(config)

            self.assertEqual(config.rotation_jitter_seconds, 0)
            self.assertEqual(config.rotation_retry_base_seconds, 30)
            self.assertEqual(config.rotation_retry_max_seconds, 900)
            self.assertEqual(instance.rotation_schedule_jitter_seconds(), 0)
            self.assertEqual(
                instance.rotation_due_at(),
                expiry - timedelta(seconds=config.rotate_before_seconds),
            )

    def test_deterministic_rotation_jitter_is_stable_bounded_and_distributed(self) -> None:
        expiry = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
        first = agent.deterministic_jitter_seconds(
            "node-entry-a",
            expiry,
            900,
            namespace="rotation-schedule",
        )
        second = agent.deterministic_jitter_seconds(
            "node-entry-a",
            expiry,
            900,
            namespace="rotation-schedule",
        )
        values = {
            agent.deterministic_jitter_seconds(
                f"node-entry-{index}",
                expiry,
                900,
                namespace="rotation-schedule",
            )
            for index in range(100)
        }

        self.assertEqual(first, second)
        self.assertGreaterEqual(first, 0)
        self.assertLessEqual(first, 900)
        self.assertGreater(len(values), 20)
        self.assertEqual(
            agent.deterministic_jitter_seconds(
                "node-entry-a",
                expiry,
                0,
                namespace="rotation-schedule",
            ),
            0,
        )

    def test_rotation_backoff_is_bounded_and_deterministic(self) -> None:
        expiry = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
        delays = [
            agent.rotation_backoff_seconds(30, 900, attempt, "node-entry-a", expiry)
            for attempt in range(1, 12)
        ]
        repeated = agent.rotation_backoff_seconds(30, 900, 4, "node-entry-a", expiry)

        self.assertEqual(repeated, delays[3])
        for attempt, delay in enumerate(delays, start=1):
            ceiling = min(900, 30 * (2 ** (attempt - 1)))
            self.assertGreaterEqual(delay, max(1, ceiling // 2))
            self.assertLessEqual(delay, ceiling)
        self.assertTrue(all(delay <= 900 for delay in delays))

    def test_pending_rotation_bypasses_initial_jitter_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "agent.env"
            expiry = datetime.now(timezone.utc) + timedelta(hours=24)
            agent.write_env_file(
                env_path,
                {
                    "WAVEMESH_API_BASE": "https://example.invalid/api",
                    "WAVEMESH_NODE_ID": "node-12345678",
                    "WAVEMESH_TENANT_ID": "tenant-12345678",
                    "WAVEMESH_AGENT_TOKEN": agent.generate_token(),
                    "WAVEMESH_AGENT_TOKEN_EXPIRES_AT": agent.format_timestamp(expiry),
                    "WAVEMESH_AGENT_MODE": "observe-only",
                    "WAVEMESH_AGENT_ROTATE_BEFORE_SECONDS": "21600",
                    "WAVEMESH_AGENT_ROTATION_JITTER_SECONDS": "900",
                },
            )
            instance = agent.NodeAgent(agent.AgentConfig.load(env_path))
            now = datetime.now(timezone.utc)

            self.assertFalse(instance.rotation_is_due(now))
            instance.load_or_create_pending_replacement()
            self.assertTrue(instance.rotation_is_due(now))

    def test_rotation_retry_state_survives_restart_without_secret_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "agent.env"
            runtime_path = Path(directory) / "runtime.json"
            expiry = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
            token = agent.generate_token()
            agent.write_env_file(
                env_path,
                {
                    "WAVEMESH_API_BASE": "https://example.invalid/api",
                    "WAVEMESH_NODE_ID": "node-12345678",
                    "WAVEMESH_TENANT_ID": "tenant-12345678",
                    "WAVEMESH_AGENT_TOKEN": token,
                    "WAVEMESH_AGENT_TOKEN_EXPIRES_AT": agent.format_timestamp(expiry),
                    "WAVEMESH_AGENT_MODE": "observe-only",
                    "WAVEMESH_AGENT_RUNTIME_PATH": str(runtime_path),
                },
            )
            now = datetime(2026, 7, 28, 6, 0, tzinfo=timezone.utc)
            first = agent.NodeAgent(agent.AgentConfig.load(env_path))
            first.schedule_rotation_retry(now, "NETWORK_ERROR", True)

            second = agent.NodeAgent(agent.AgentConfig.load(env_path))
            retry_at = second.rotation_retry_at()
            encoded = runtime_path.read_text(encoding="utf-8")

            self.assertIsNotNone(retry_at)
            self.assertGreater(retry_at, now)
            self.assertEqual(second.runtime["rotation_retry_attempts"], 1)
            self.assertEqual(second.runtime["rotation_retry_code"], "NETWORK_ERROR")
            self.assertNotIn(token, encoded)
            self.assertNotIn(agent.token_hash(token), encoded)
            assert_posix_mode(self, runtime_path, 0o600)

    def test_rotation_retry_state_is_cleared_after_credential_expiry_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "agent.env"
            runtime_path = Path(directory) / "runtime.json"
            initial_expiry = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
            agent.write_env_file(
                env_path,
                {
                    "WAVEMESH_API_BASE": "https://example.invalid/api",
                    "WAVEMESH_NODE_ID": "node-12345678",
                    "WAVEMESH_TENANT_ID": "tenant-12345678",
                    "WAVEMESH_AGENT_TOKEN": agent.generate_token(),
                    "WAVEMESH_AGENT_TOKEN_EXPIRES_AT": agent.format_timestamp(initial_expiry),
                    "WAVEMESH_AGENT_MODE": "observe-only",
                    "WAVEMESH_AGENT_RUNTIME_PATH": str(runtime_path),
                },
            )
            instance = agent.NodeAgent(agent.AgentConfig.load(env_path))
            instance.schedule_rotation_retry(
                datetime(2026, 7, 28, 6, 0, tzinfo=timezone.utc),
                "NETWORK_ERROR",
                True,
            )
            instance.config.token_expires_at = initial_expiry + timedelta(hours=24)

            self.assertIsNone(instance.rotation_retry_at())
            self.assertNotIn("rotation_retry_at", instance.runtime)
            self.assertNotIn("rotation_retry_attempts", instance.runtime)

    def test_forbidden_observation_key_is_rejected(self) -> None:
        with self.assertRaises(agent.AgentError):
            agent.assert_redacted({"api_token": "not-allowed"})

    def test_mtls_defaults_disabled_without_changing_bearer_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "agent.env"
            token = agent.generate_token()
            agent.write_env_file(
                env_path,
                {
                    "WAVEMESH_API_BASE": "https://example.invalid/api",
                    "WAVEMESH_NODE_ID": "node-12345678",
                    "WAVEMESH_TENANT_ID": "tenant-12345678",
                    "WAVEMESH_AGENT_TOKEN": token,
                    "WAVEMESH_AGENT_TOKEN_EXPIRES_AT": agent.format_timestamp(
                        datetime.now(timezone.utc) + timedelta(hours=24)
                    ),
                    "WAVEMESH_AGENT_MODE": "observe-only",
                },
            )

            config = agent.AgentConfig.load(env_path)

            self.assertEqual(config.mtls_mode, "disabled")
            self.assertIsNone(config.mtls_api_base)
            self.assertEqual(config.agent_token, token)
            self.assertIsNone(agent.build_mtls_runtime(config))

    def test_bearer_observation_failure_preserves_health_for_mtls_heartbeat(self) -> None:
        class FakeStatus:
            def capability(self):
                return {
                    "mode": "shadow",
                    "state": "SHADOW_ACTIVE",
                    "retry_attempts": 0,
                    "retry_at": None,
                    "code": None,
                }

        class FakeMtlsRuntime:
            def __init__(self):
                self.heartbeats = []

            def lifecycle_cycle(self, version):
                return FakeStatus()

            def api_json(self, method, path, payload, expected):
                return {}

            def shadow_heartbeat(self, payload):
                self.heartbeats.append(payload)
                return FakeStatus()

        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "agent.env"
            runtime_path = Path(directory) / "runtime.json"
            agent.write_env_file(
                env_path,
                {
                    "WAVEMESH_API_BASE": "https://example.invalid/api",
                    "WAVEMESH_NODE_ID": "node-12345678",
                    "WAVEMESH_TENANT_ID": "tenant-12345678",
                    "WAVEMESH_AGENT_TOKEN": agent.generate_token(),
                    "WAVEMESH_AGENT_TOKEN_EXPIRES_AT": agent.format_timestamp(
                        datetime.now(timezone.utc) + timedelta(hours=24)
                    ),
                    "WAVEMESH_AGENT_MODE": "observe-only",
                    "WAVEMESH_AGENT_RUNTIME_PATH": str(runtime_path),
                    "WAVEMESH_AGENT_MTLS_MODE": "shadow",
                    "WAVEMESH_AGENT_MTLS_API_BASE": "https://mtls.example.invalid/api",
                    "WAVEMESH_AGENT_COMMAND_MODE": "access",
                },
            )
            mtls_runtime = FakeMtlsRuntime()
            instance = agent.NodeAgent(
                agent.AgentConfig.load(env_path),
                mtls_runtime=mtls_runtime,
            )

            def fail_bearer_observation_delivery():
                instance.last_health_state = {
                    "mode": "observe_only",
                    "node_status": "healthy",
                    "healthy_exits": 1,
                    "total_exits": 1,
                    "routes": [],
                    "auto_routes": [],
                }
                raise agent.ApiError(401, "UNAUTHORIZED", False)

            with (
                mock.patch.object(instance, "rotate_if_due"),
                mock.patch.object(
                    instance,
                    "collect_and_send_observation",
                    side_effect=fail_bearer_observation_delivery,
                ),
                mock.patch.object(
                    instance,
                    "send_heartbeat",
                    side_effect=agent.ApiError(401, "UNAUTHORIZED", False),
                ),
            ):
                instance.run(once=True)

            self.assertEqual(len(mtls_runtime.heartbeats), 1)
            heartbeat = mtls_runtime.heartbeats[0]
            self.assertEqual(heartbeat["status"], "active")
            self.assertTrue(heartbeat["capabilities"]["command_polling"])
            self.assertTrue(heartbeat["capabilities"]["command_execution"])
            self.assertEqual(instance.last_health_state["node_status"], "healthy")

    def test_shadow_failure_does_not_block_bearer_foreground_cycle(self) -> None:
        class FakeStatus:
            def __init__(self, state):
                self.state = state

            def capability(self):
                return {
                    "mode": "shadow",
                    "state": self.state,
                    "retry_attempts": 0,
                    "retry_at": None,
                    "code": None,
                }

        class FakeMtlsRuntime:
            def __init__(self):
                self.lifecycle_calls = 0
                self.shadow_calls = 0

            def lifecycle_cycle(self, version):
                self.lifecycle_calls += 1
                return FakeStatus("SHADOW_READY")

            def shadow_heartbeat(self, payload):
                self.shadow_calls += 1
                raise RuntimeError("simulated mTLS transport failure")

        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "agent.env"
            runtime_path = Path(directory) / "runtime.json"
            agent.write_env_file(
                env_path,
                {
                    "WAVEMESH_API_BASE": "https://example.invalid/api",
                    "WAVEMESH_NODE_ID": "node-12345678",
                    "WAVEMESH_TENANT_ID": "tenant-12345678",
                    "WAVEMESH_AGENT_TOKEN": agent.generate_token(),
                    "WAVEMESH_AGENT_TOKEN_EXPIRES_AT": agent.format_timestamp(
                        datetime.now(timezone.utc) + timedelta(hours=24)
                    ),
                    "WAVEMESH_AGENT_MODE": "observe-only",
                    "WAVEMESH_AGENT_RUNTIME_PATH": str(runtime_path),
                },
            )
            mtls_runtime = FakeMtlsRuntime()
            instance = agent.NodeAgent(
                agent.AgentConfig.load(env_path),
                mtls_runtime=mtls_runtime,
            )
            bearer_heartbeats = []

            with (
                mock.patch.object(instance, "rotate_if_due"),
                mock.patch.object(instance, "collect_and_send_observation"),
                mock.patch.object(
                    instance,
                    "send_heartbeat",
                    side_effect=lambda payload: bearer_heartbeats.append(payload),
                ),
            ):
                instance.run(once=True)

            self.assertEqual(mtls_runtime.lifecycle_calls, 1)
            self.assertEqual(mtls_runtime.shadow_calls, 1)
            self.assertEqual(len(bearer_heartbeats), 1)
            self.assertEqual(
                bearer_heartbeats[0]["capabilities"]["mtls_shadow"]["state"],
                "SHADOW_READY",
            )
            self.assertEqual(instance.last_mtls_status["state"], "BLOCKED")
            self.assertNotIn("simulated", json.dumps(instance.last_mtls_status))


if __name__ == "__main__":
    unittest.main()
