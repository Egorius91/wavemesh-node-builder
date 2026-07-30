#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
sys.path.insert(0, str(AGENT_DIR))

MODULE_PATH = AGENT_DIR / "node_mtls_runtime.py"
SPEC = importlib.util.spec_from_file_location("node_mtls_runtime_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)

TOKEN = "wvn_" + "a" * 40
NODE_ID = "node_12345678"
TENANT_ID = "tenant_12345678"
NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)


class FakeState:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.active = None
        self.active_hash = None
        self.pending_ack = None

    def active_identity(self, expected_identity_uri=None):
        self.expected_identity_uri = expected_identity_uri
        return self.active

    def active_request_hash(self, active=None):
        return self.active_hash

    def pending_acknowledgement(self):
        return self.pending_ack


class LifecycleHarness:
    def __init__(self, state: FakeState, failure=None) -> None:
        self.state = state
        self.failure = failure
        self.calls = []
        self.request_hash = "b" * 64

    def factory(self, config, state=None):
        harness = self

        class FakeLifecycle:
            def issue_or_rotate(self, agent_version):
                harness.calls.append(("issue", config.auth_mode, agent_version))
                if harness.failure is not None:
                    raise harness.failure
                harness.state.active = object()
                harness.state.active_hash = harness.request_hash
                harness.state.pending_ack = SimpleNamespace(
                    credential_id="credential_mtls_123",
                    request_hash=harness.request_hash,
                    delivery_expires_at=NOW + timedelta(minutes=15),
                )
                return lifecycle_result()

            def retrieve(self, credential_id):
                harness.calls.append(("retrieve", config.auth_mode, credential_id))
                if harness.failure is not None:
                    raise harness.failure
                harness.state.active = object()
                harness.state.active_hash = harness.state.pending_ack.request_hash
                return lifecycle_result()

            def acknowledge(self, credential_id):
                harness.calls.append(("acknowledge", config.auth_mode, credential_id))
                if harness.failure is not None:
                    raise harness.failure
                harness.state.pending_ack = None
                return NOW

        return FakeLifecycle()


def lifecycle_result():
    return runtime.CertificateLifecycleResult(
        credential_id="credential_mtls_123",
        expires_at=NOW + timedelta(days=1),
        delivery_expires_at=NOW + timedelta(minutes=15),
        previous_valid_until=None,
        already_processed=False,
        generation="a" * 24,
    )


def configured(root: Path, **overrides):
    values = {
        "mode": "shadow",
        "bearer_api_base": "https://api.example.invalid/api",
        "mtls_api_base": "https://mtls.example.invalid/api",
        "node_id": NODE_ID,
        "tenant_id": TENANT_ID,
        "environment": "staging",
        "bearer_token": TOKEN,
        "state_root": root,
        "retry_base_seconds": 5,
        "retry_max_seconds": 30,
        "retry_max_attempts": 3,
        "retry_jitter_seconds": 0,
    }
    values.update(overrides)
    return runtime.MtlsRuntimeConfig(**values)


class NodeMtlsRuntimeTests(unittest.TestCase):
    def test_disabled_mode_is_bearer_only_and_does_not_touch_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tls"
            instance = runtime.NodeMtlsRuntime(
                runtime.MtlsRuntimeConfig(
                    mode="disabled",
                    bearer_api_base="http://legacy.example.invalid/api",
                    mtls_api_base=None,
                    node_id=NODE_ID,
                    tenant_id=TENANT_ID,
                    environment="staging",
                    bearer_token=TOKEN,
                    state_root=root,
                ),
                state=FakeState(root),
            )

            self.assertEqual(
                instance.lifecycle_cycle("0.3.0-mtls-shadow").state,
                runtime.MtlsAgentState.BEARER_ONLY,
            )
            self.assertFalse(root.exists())

    def test_initial_enrollment_acknowledges_then_activates_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tls"
            state = FakeState(root)
            harness = LifecycleHarness(state)
            instance = runtime.NodeMtlsRuntime(configured(root), state=state)

            with (
                mock.patch.object(runtime, "NodeCertificateLifecycleClient", side_effect=harness.factory),
                mock.patch.object(runtime, "parse_certificate_expiry", return_value=NOW + timedelta(days=1)),
            ):
                status = instance.lifecycle_cycle("0.3.0-mtls-shadow", NOW)

            self.assertEqual(status.state, runtime.MtlsAgentState.SHADOW_READY)
            self.assertEqual([call[0:2] for call in harness.calls], [
                ("issue", "bearer"),
                ("acknowledge", "bearer"),
            ])
            self.assertIsNone(state.pending_ack)

            transport = mock.Mock()
            transport.api_json.return_value = {}
            with (
                mock.patch.object(instance, "_mtls_transport", return_value=transport),
                mock.patch.object(runtime, "parse_certificate_expiry", return_value=NOW + timedelta(days=1)),
            ):
                status = instance.shadow_heartbeat({"agent_version": "sanitized"}, NOW)

            self.assertEqual(status.state, runtime.MtlsAgentState.SHADOW_ACTIVE)
            transport.api_json.assert_called_once()

    def test_pending_delivery_is_retrieved_after_restart_before_ack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tls"
            state = FakeState(root)
            state.active = object()
            state.active_hash = "a" * 64
            state.pending_ack = SimpleNamespace(
                credential_id="credential_mtls_123",
                request_hash="b" * 64,
                delivery_expires_at=NOW + timedelta(minutes=10),
            )
            harness = LifecycleHarness(state)
            instance = runtime.NodeMtlsRuntime(configured(root), state=state)

            with (
                mock.patch.object(runtime, "NodeCertificateLifecycleClient", side_effect=harness.factory),
                mock.patch.object(runtime, "parse_certificate_expiry", return_value=NOW + timedelta(days=1)),
            ):
                status = instance.lifecycle_cycle("0.3.0-mtls-shadow", NOW)

            self.assertEqual(status.state, runtime.MtlsAgentState.SHADOW_READY)
            self.assertEqual([call[0:2] for call in harness.calls], [
                ("retrieve", "bearer"),
                ("acknowledge", "bearer"),
            ])

    def test_expired_delivery_is_acknowledged_when_matching_identity_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tls"
            state = FakeState(root)
            state.active = object()
            state.active_hash = "b" * 64
            state.pending_ack = SimpleNamespace(
                credential_id="credential_mtls_123",
                request_hash="b" * 64,
                delivery_expires_at=NOW - timedelta(minutes=10),
            )
            harness = LifecycleHarness(state)
            instance = runtime.NodeMtlsRuntime(configured(root), state=state)

            with (
                mock.patch.object(runtime, "NodeCertificateLifecycleClient", side_effect=harness.factory),
                mock.patch.object(runtime, "parse_certificate_expiry", return_value=NOW + timedelta(days=1)),
            ):
                status = instance.lifecycle_cycle("0.4.0-mtls-shadow", NOW)

            self.assertEqual(status.state, runtime.MtlsAgentState.SHADOW_READY)
            self.assertEqual([call[0:2] for call in harness.calls], [
                ("acknowledge", "bearer"),
            ])
            self.assertIsNone(state.pending_ack)

    def test_expired_delivery_blocks_when_matching_identity_is_not_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tls"
            state = FakeState(root)
            state.active = object()
            state.active_hash = "a" * 64
            state.pending_ack = SimpleNamespace(
                credential_id="credential_mtls_123",
                request_hash="b" * 64,
                delivery_expires_at=NOW - timedelta(minutes=10),
            )
            harness = LifecycleHarness(state)
            instance = runtime.NodeMtlsRuntime(configured(root), state=state)

            with (
                mock.patch.object(runtime, "NodeCertificateLifecycleClient", side_effect=harness.factory),
                mock.patch.object(runtime, "parse_certificate_expiry", return_value=NOW + timedelta(days=1)),
            ):
                status = instance.lifecycle_cycle("0.4.0-mtls-shadow", NOW)

            self.assertEqual(status.state, runtime.MtlsAgentState.BLOCKED)
            self.assertEqual(harness.calls, [])
            self.assertIsNotNone(state.pending_ack)

    def test_due_rotation_uses_mtls_but_ack_keeps_bearer_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tls"
            state = FakeState(root)
            state.active = object()
            state.active_hash = "a" * 64
            harness = LifecycleHarness(state)
            instance = runtime.NodeMtlsRuntime(
                configured(root, rotate_before_seconds=2 * 60 * 60),
                state=state,
            )

            with (
                mock.patch.object(runtime, "NodeCertificateLifecycleClient", side_effect=harness.factory),
                mock.patch.object(runtime, "parse_certificate_expiry", return_value=NOW + timedelta(hours=1)),
            ):
                status = instance.lifecycle_cycle("0.3.0-mtls-shadow", NOW)

            self.assertEqual(status.state, runtime.MtlsAgentState.SHADOW_READY)
            self.assertEqual([call[0:2] for call in harness.calls], [
                ("issue", "mtls"),
                ("acknowledge", "bearer"),
            ])

    def test_retryable_failure_has_finite_zero_jitter_backoff_then_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tls"
            state = FakeState(root)
            failure = runtime.MtlsApiError(503, "ISSUER_UNAVAILABLE", True)
            harness = LifecycleHarness(state, failure=failure)
            instance = runtime.NodeMtlsRuntime(configured(root), state=state)

            with mock.patch.object(
                runtime,
                "NodeCertificateLifecycleClient",
                side_effect=harness.factory,
            ):
                first = instance.lifecycle_cycle("0.3.0-mtls-shadow", NOW)
                second = instance.lifecycle_cycle(
                    "0.3.0-mtls-shadow",
                    NOW + timedelta(seconds=5),
                )
                third = instance.lifecycle_cycle(
                    "0.3.0-mtls-shadow",
                    NOW + timedelta(seconds=15),
                )
                unchanged = instance.lifecycle_cycle(
                    "0.3.0-mtls-shadow",
                    NOW + timedelta(hours=1),
                )

            self.assertEqual(first.retry_at, NOW + timedelta(seconds=5))
            self.assertEqual(second.retry_at, NOW + timedelta(seconds=15))
            self.assertEqual(third.state, runtime.MtlsAgentState.BLOCKED)
            self.assertEqual(third.retry_attempts, 3)
            self.assertEqual(unchanged, third)
            self.assertEqual(len([call for call in harness.calls if call[0] == "issue"]), 3)
            encoded = instance.runtime_path.read_text(encoding="utf-8")
            self.assertNotIn(TOKEN, encoded)
            self.assertNotIn("BEGIN CERTIFICATE", encoded)

    def test_nonretryable_failure_blocks_without_request_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tls"
            state = FakeState(root)
            harness = LifecycleHarness(
                state,
                failure=runtime.MtlsApiError(409, "REQUEST_CONFLICT", False),
            )
            instance = runtime.NodeMtlsRuntime(configured(root), state=state)

            with mock.patch.object(
                runtime,
                "NodeCertificateLifecycleClient",
                side_effect=harness.factory,
            ):
                first = instance.lifecycle_cycle("0.3.0-mtls-shadow", NOW)
                second = instance.lifecycle_cycle(
                    "0.3.0-mtls-shadow",
                    NOW + timedelta(days=1),
                )

            self.assertEqual(first.state, runtime.MtlsAgentState.BLOCKED)
            self.assertEqual(second, first)
            self.assertEqual(len(harness.calls), 1)


if __name__ == "__main__":
    unittest.main()
