#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import node_mtls_runtime as runtime  # noqa: E402


class FakeState:
    def active_identity(self, _expected_identity_uri: str):
        return object()


class PrimaryMtlsHeartbeatActivationTests(unittest.TestCase):
    def _runtime(self, directory: str) -> runtime.NodeMtlsRuntime:
        config = runtime.MtlsRuntimeConfig(
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
        instance = runtime.NodeMtlsRuntime(config, state=FakeState())
        instance._clear_retry(runtime.MtlsAgentState.SHADOW_READY)
        return instance

    def test_successful_primary_heartbeat_promotes_shadow_ready_to_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = self._runtime(directory)
            transport = mock.Mock()
            transport.api_json.return_value = {}

            with mock.patch.object(
                runtime,
                "parse_certificate_expiry",
                return_value=datetime.now(timezone.utc) + timedelta(hours=1),
            ), mock.patch.object(instance, "_mtls_transport", return_value=transport):
                result = instance.api_json(
                    "POST",
                    "internal/v1/nodes/node-12345678/heartbeat",
                    {"status": "active"},
                    expected=(204,),
                )

            self.assertEqual(result, {})
            self.assertEqual(instance.status().state, runtime.MtlsAgentState.SHADOW_ACTIVE)
            transport.api_json.assert_called_once()

    def test_non_heartbeat_request_does_not_promote_shadow_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = self._runtime(directory)
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
                    {"type": "node.health.test"},
                    expected=(202,),
                )

            self.assertEqual(instance.status().state, runtime.MtlsAgentState.SHADOW_READY)
            transport.api_json.assert_called_once()


if __name__ == "__main__":
    unittest.main()
