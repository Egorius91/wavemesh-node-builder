#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import node_agent as agent  # noqa: E402
import node_mtls_client as mtls_client  # noqa: E402
import node_mtls_runtime as runtime  # noqa: E402


class FakeStatus:
    def __init__(self, state: str = "SHADOW_ACTIVE") -> None:
        self.state = SimpleNamespace(value=state)
        self._state = state

    def capability(self) -> dict[str, object]:
        return {
            "mode": "shadow",
            "state": self._state,
            "retry_attempts": 0,
            "retry_at": None,
            "code": None,
        }


class FakePrimaryRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.shadow_calls = 0
        self.error: Exception | None = None

    def api_json(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        expected: tuple[int, ...],
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        self.calls.append(
            {
                "method": method,
                "path": path,
                "payload": payload,
                "expected": expected,
                "headers": dict(headers or {}),
            }
        )
        return {}

    def lifecycle_cycle(self, _agent_version: str) -> FakeStatus:
        return FakeStatus()

    def shadow_heartbeat(self, _payload: dict[str, object]) -> FakeStatus:
        self.shadow_calls += 1
        return FakeStatus()

    def status(self) -> FakeStatus:
        return FakeStatus()


class FakeHttpResponse:
    status = 204

    def read(self) -> bytes:
        return b""

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback


class PrimaryMtlsAgentTests(unittest.TestCase):
    def _write_config(
        self,
        directory: str,
        *,
        auth_mode: str,
        with_bearer: bool,
        mtls_mode: str = "shadow",
    ) -> Path:
        env_path = Path(directory) / "agent.env"
        values = {
            "WAVEMESH_API_BASE": "https://api.example.invalid/api",
            "WAVEMESH_NODE_ID": "node-12345678",
            "WAVEMESH_TENANT_ID": "tenant-12345678",
            "WAVEMESH_AGENT_MODE": "observe-only",
            "WAVEMESH_AGENT_AUTH_MODE": auth_mode,
            "WAVEMESH_AGENT_MTLS_MODE": mtls_mode,
            "WAVEMESH_AGENT_MTLS_API_BASE": "https://mtls.example.invalid/api",
            "WAVEMESH_AGENT_MTLS_ENVIRONMENT": "staging",
            "WAVEMESH_AGENT_MTLS_STATE_ROOT": str(Path(directory) / "tls"),
            "WAVEMESH_AGENT_RUNTIME_PATH": str(Path(directory) / "runtime.json"),
        }
        if with_bearer:
            values["WAVEMESH_AGENT_TOKEN"] = agent.generate_token()
            values["WAVEMESH_AGENT_TOKEN_EXPIRES_AT"] = agent.format_timestamp(
                datetime.now(timezone.utc) + timedelta(hours=24)
            )
        agent.write_env_file(env_path, values)
        return env_path

    def test_bearer_default_still_requires_bearer_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "agent.env"
            agent.write_env_file(
                env_path,
                {
                    "WAVEMESH_API_BASE": "https://api.example.invalid/api",
                    "WAVEMESH_NODE_ID": "node-12345678",
                    "WAVEMESH_TENANT_ID": "tenant-12345678",
                },
            )
            with self.assertRaises(agent.AgentError):
                agent.AgentConfig.load(env_path)

            values = agent.read_env_file(env_path)
            values["WAVEMESH_AGENT_TOKEN"] = agent.generate_token()
            values["WAVEMESH_AGENT_TOKEN_EXPIRES_AT"] = agent.format_timestamp(
                datetime.now(timezone.utc) + timedelta(hours=24)
            )
            agent.write_env_file(env_path, values)
            config = agent.AgentConfig.load(env_path)
            self.assertEqual(config.auth_mode, "bearer")
            self.assertIsNotNone(config.agent_token)

    def test_pure_mtls_needs_no_bearer_and_disables_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = agent.AgentConfig.load(
                self._write_config(directory, auth_mode="mtls", with_bearer=False)
            )
            self.assertEqual(config.auth_mode, "mtls")
            self.assertIsNone(config.agent_token)
            self.assertIsNone(config.token_expires_at)

            mtls_runtime = agent.build_mtls_runtime(config)
            self.assertIsNotNone(mtls_runtime)
            self.assertFalse(mtls_runtime.config.bearer_bootstrap_enabled)
            self.assertIsNone(mtls_runtime.config.bearer_token)
            self.assertIsNone(mtls_runtime.config.bearer_api_base)

    def test_pure_mtls_does_not_retain_legacy_bearer_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = agent.AgentConfig.load(
                self._write_config(directory, auth_mode="mtls", with_bearer=True)
            )
            self.assertIsNone(config.agent_token)
            self.assertIsNone(config.token_expires_at)

    def test_primary_mtls_routes_request_and_headers_without_bearer_urlopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = agent.AgentConfig.load(
                self._write_config(directory, auth_mode="mtls", with_bearer=False)
            )
            primary = FakePrimaryRuntime()
            instance = agent.NodeAgent(config, mtls_runtime=primary)

            with mock.patch.object(
                agent.request,
                "urlopen",
                side_effect=AssertionError("bearer urlopen must not run"),
            ):
                instance.api_json(
                    "POST",
                    "internal/v1/nodes/node-12345678/observations",
                    {"safe": True},
                    expected=(202,),
                    headers={"Idempotency-Key": "i"},
                )

            self.assertEqual(len(primary.calls), 1)
            self.assertEqual(primary.calls[0]["headers"], {"Idempotency-Key": "i"})

    def test_mtls_network_error_never_downgrades_to_bearer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = agent.AgentConfig.load(
                self._write_config(directory, auth_mode="mtls", with_bearer=True)
            )
            primary = FakePrimaryRuntime()
            primary.error = mtls_client.MtlsApiError(0, "NETWORK_OR_TLS_ERROR", True)
            instance = agent.NodeAgent(config, mtls_runtime=primary)

            with mock.patch.object(
                agent.request,
                "urlopen",
                side_effect=AssertionError("bearer fallback must not run"),
            ):
                with self.assertRaises(mtls_client.MtlsApiError):
                    instance.api_json(
                        "POST",
                        "internal/v1/nodes/node-12345678/heartbeat",
                        {},
                        expected=(204,),
                    )

    def test_bootstrap_fallback_is_only_for_local_identity_unavailability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = agent.AgentConfig.load(
                self._write_config(
                    directory,
                    auth_mode="bootstrap-mtls",
                    with_bearer=True,
                )
            )
            primary = FakePrimaryRuntime()
            primary.error = runtime.MtlsRuntimeError("identity unavailable")
            instance = agent.NodeAgent(config, mtls_runtime=primary)
            captured: list[object] = []

            def open_request(req, **_kwargs):
                captured.append(req)
                return FakeHttpResponse()

            with mock.patch.object(agent.request, "urlopen", side_effect=open_request):
                instance.api_json(
                    "POST",
                    "internal/v1/nodes/node-12345678/heartbeat",
                    {},
                    expected=(204,),
                )

            self.assertEqual(len(captured), 1)
            self.assertTrue(captured[0].get_header("Authorization").startswith("Bearer wvn_"))

            primary.error = mtls_client.MtlsApiError(0, "NETWORK_OR_TLS_ERROR", True)
            with mock.patch.object(
                agent.request,
                "urlopen",
                side_effect=AssertionError("active mTLS errors must not downgrade"),
            ):
                with self.assertRaises(mtls_client.MtlsApiError):
                    instance.api_json(
                        "POST",
                        "internal/v1/nodes/node-12345678/heartbeat",
                        {},
                        expected=(204,),
                    )

    def test_mtls_skips_bearer_rotation_and_duplicate_shadow_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = agent.AgentConfig.load(
                self._write_config(directory, auth_mode="mtls", with_bearer=False)
            )
            primary = FakePrimaryRuntime()
            instance = agent.NodeAgent(config, mtls_runtime=primary)

            instance.rotate_if_due()
            self.assertFalse(config.pending_rotation_path.exists())
            instance.run_mtls_shadow_heartbeat({"status": "active"})
            self.assertEqual(primary.shadow_calls, 0)

    def test_primary_mtls_requires_shadow_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = self._write_config(
                directory,
                auth_mode="mtls",
                with_bearer=False,
                mtls_mode="disabled",
            )
            with self.assertRaises(agent.AgentError):
                agent.AgentConfig.load(env_path)


class FakeLifecycleState:
    def __init__(self, active=None, pending=None, active_request_hash: str | None = None) -> None:
        self.active = active
        self.pending = pending
        self.request_hash = active_request_hash

    def active_identity(self, _expected_identity_uri: str):
        return self.active

    def pending_acknowledgement(self):
        return self.pending

    def active_request_hash(self, _active) -> str | None:
        return self.request_hash


class FakeLifecycleClient:
    def __init__(self) -> None:
        self.retrieved: list[str] = []
        self.acknowledged: list[str] = []

    def retrieve(self, credential_id: str) -> None:
        self.retrieved.append(credential_id)

    def acknowledge(self, credential_id: str) -> None:
        self.acknowledged.append(credential_id)


class PrimaryMtlsRuntimeTests(unittest.TestCase):
    def _config(self, directory: str) -> runtime.MtlsRuntimeConfig:
        return runtime.MtlsRuntimeConfig(
            mode="shadow",
            bearer_api_base=None,
            mtls_api_base="https://mtls.example.invalid/api",
            node_id="node-12345678",
            tenant_id="tenant-12345678",
            environment="staging",
            bearer_token=None,
            state_root=Path(directory) / "tls",
            bearer_bootstrap_enabled=False,
        )

    def test_missing_active_identity_blocks_without_bearer_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = runtime.NodeMtlsRuntime(
                self._config(directory),
                state=FakeLifecycleState(),
            )
            with mock.patch.object(
                instance,
                "_bearer_lifecycle_client",
                side_effect=AssertionError("bearer lifecycle must not run"),
            ):
                result = instance.lifecycle_cycle(
                    "test",
                    now=datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc),
                )
            self.assertEqual(result.state, runtime.MtlsAgentState.BLOCKED)
            self.assertEqual(result.code, "MTLSRUNTIMEERROR")

    def test_pending_retrieval_uses_valid_active_mtls_when_bearer_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pending = SimpleNamespace(
                credential_id="credential_12345678",
                request_hash="b" * 64,
                delivery_expires_at=datetime(2026, 8, 26, 11, 0, tzinfo=timezone.utc),
            )
            state = FakeLifecycleState(
                active=object(),
                pending=pending,
                active_request_hash="a" * 64,
            )
            instance = runtime.NodeMtlsRuntime(self._config(directory), state=state)
            lifecycle = FakeLifecycleClient()

            with mock.patch.object(
                runtime,
                "parse_certificate_expiry",
                return_value=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
            ), mock.patch.object(
                instance,
                "_mtls_lifecycle_client",
                return_value=lifecycle,
            ), mock.patch.object(
                instance,
                "_bearer_lifecycle_client",
                side_effect=AssertionError("bearer retrieval must not run"),
            ):
                result = instance.lifecycle_cycle(
                    "test",
                    now=datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc),
                )

            self.assertEqual(lifecycle.retrieved, ["credential_12345678"])
            self.assertEqual(lifecycle.acknowledged, ["credential_12345678"])
            self.assertEqual(result.state, runtime.MtlsAgentState.SHADOW_READY)

    def test_runtime_forwards_request_headers_to_mtls_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = runtime.NodeMtlsRuntime(
                self._config(directory),
                state=FakeLifecycleState(active=object()),
            )
            transport = mock.Mock()
            transport.api_json.return_value = {}

            with mock.patch.object(
                runtime,
                "parse_certificate_expiry",
                return_value=datetime.now(timezone.utc) + timedelta(hours=1),
            ), mock.patch.object(instance, "_mtls_transport", return_value=transport):
                instance.api_json(
                    "POST",
                    "internal/v1/nodes/node-12345678/observations",
                    {"safe": True},
                    expected=(202,),
                    headers={"Idempotency-Key": "i"},
                )

            transport.api_json.assert_called_once_with(
                "POST",
                "internal/v1/nodes/node-12345678/observations",
                {"safe": True},
                expected=(202,),
                headers={"Idempotency-Key": "i"},
            )


if __name__ == "__main__":
    unittest.main()
