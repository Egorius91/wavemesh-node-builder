#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest import mock
from urllib import error

ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "agent" / "node_mtls_state.py"
STATE_SPEC = importlib.util.spec_from_file_location("node_mtls_state", STATE_PATH)
assert STATE_SPEC and STATE_SPEC.loader
state_module = importlib.util.module_from_spec(STATE_SPEC)
sys.modules[STATE_SPEC.name] = state_module
STATE_SPEC.loader.exec_module(state_module)

CLIENT_PATH = ROOT / "agent" / "node_mtls_client.py"
CLIENT_SPEC = importlib.util.spec_from_file_location("node_mtls_client", CLIENT_PATH)
assert CLIENT_SPEC and CLIENT_SPEC.loader
client = importlib.util.module_from_spec(CLIENT_SPEC)
sys.modules[CLIENT_SPEC.name] = client
CLIENT_SPEC.loader.exec_module(client)

TOKEN = "wvn_" + "a" * 40
NODE_ID = "node_12345678"
TENANT_ID = "tenant_12345678"


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status = status
        self.body = b"" if payload is None else json.dumps(payload).encode("utf-8")

    def read(self, amount: int = -1) -> bytes:
        return self.body if amount < 0 else self.body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeState:
    def __init__(self, active=None) -> None:
        self.active = active
        self.pending = state_module.PendingCertificateRequest(
            csr_pem=(
                "-----BEGIN CERTIFICATE REQUEST-----\n"
                "VEVTVC1PTkxZLUNFUklGSUNBVEUtUkVRVUVTVA==\n"
                "-----END CERTIFICATE REQUEST-----"
            ),
            request_hash="1" * 64,
            public_key_hash="2" * 64,
            created_at="2026-07-27T08:00:00.000Z",
        )
        self.activation = None

    def active_identity(self):
        return self.active

    def prepare_pending_request(self):
        return self.pending

    def activate_pending_certificate(self, certificate, chain, identity_uri):
        self.activation = (certificate, chain, identity_uri)
        self.active = state_module.ActiveIdentity(
            generation="a" * 24,
            directory=Path("/private/generation"),
            private_key=Path("/private/generation/client.key"),
            certificate=Path("/private/generation/client.crt"),
            ca_bundle=Path("/private/generation/ca.crt"),
            metadata=Path("/private/generation/metadata.json"),
        )
        return self.active


def config(auth_mode="bearer", bearer=TOKEN):
    return client.MtlsClientConfig(
        api_base="https://node-api.example.invalid/api",
        node_id=NODE_ID,
        tenant_id=TENANT_ID,
        environment="staging",
        auth_mode=auth_mode,
        bearer_token=bearer,
    )


class NodeMtlsClientTests(unittest.TestCase):
    def test_current_bearer_default_remains_valid(self) -> None:
        configured = config()
        self.assertEqual(configured.auth_mode, "bearer")
        self.assertEqual(
            configured.expected_identity_uri,
            f"spiffe://wavevpn/staging/tenant/{TENANT_ID}/node/{NODE_ID}",
        )
        with self.assertRaises(client.MtlsClientError):
            config("mtls", bearer="not-required-but-invalid")

    def test_bearer_transport_includes_authorization(self) -> None:
        captured = {}

        def opener(req, **kwargs):
            captured["authorization"] = req.get_header("Authorization")
            captured["tenant"] = req.get_header("X-wavevpn-tenant-id")
            captured["context"] = kwargs["context"]
            return FakeResponse(204)

        transport = client.NodeMtlsTransport(config(), FakeState(), opener)
        result = transport.api_json("POST", f"internal/v1/nodes/{NODE_ID}/heartbeat", {}, expected=(204,))

        self.assertEqual(result, {})
        self.assertEqual(captured["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(captured["tenant"], TENANT_ID)
        self.assertIsInstance(captured["context"], ssl.SSLContext)

    def test_bootstrap_mode_switches_to_mtls_after_local_activation(self) -> None:
        state = FakeState()
        auth_headers = []
        contexts = []

        def opener(req, **kwargs):
            auth_headers.append(req.get_header("Authorization"))
            contexts.append(kwargs["context"])
            return FakeResponse(204)

        transport = client.NodeMtlsTransport(config("bootstrap-mtls"), state, opener)
        transport.api_json("POST", "first", {}, expected=(204,))
        state.active = state.activate_pending_certificate("certificate", "chain", config("bootstrap-mtls").expected_identity_uri)
        sentinel_context = object()
        with mock.patch.object(client, "build_mtls_ssl_context", return_value=sentinel_context):
            transport.api_json("POST", "second", {}, expected=(204,))

        self.assertEqual(auth_headers, [f"Bearer {TOKEN}", None])
        self.assertIs(contexts[1], sentinel_context)

    def test_mtls_mode_never_falls_back_to_bearer(self) -> None:
        called = 0

        def opener(req, **kwargs):
            nonlocal called
            called += 1
            raise error.URLError("unreachable")

        missing = client.NodeMtlsTransport(config("mtls", bearer=None), FakeState(), opener)
        with self.assertRaises(client.MtlsClientError):
            missing.api_json("POST", "heartbeat", {}, expected=(204,))
        self.assertEqual(called, 0)

        state = FakeState(
            state_module.ActiveIdentity(
                generation="b" * 24,
                directory=Path("/private/generation"),
                private_key=Path("/private/generation/client.key"),
                certificate=Path("/private/generation/client.crt"),
                ca_bundle=Path("/private/generation/ca.crt"),
                metadata=Path("/private/generation/metadata.json"),
            )
        )
        transport = client.NodeMtlsTransport(config("mtls", bearer=None), state, opener)
        with mock.patch.object(client, "build_mtls_ssl_context", return_value=object()):
            with self.assertRaises(client.MtlsApiError) as raised:
                transport.api_json("POST", "heartbeat", {}, expected=(204,))
        self.assertEqual(raised.exception.code, "NETWORK_OR_TLS_ERROR")
        self.assertEqual(called, 1)

    def test_certificate_bootstrap_reuses_pending_csr_and_activates_exact_identity(self) -> None:
        state = FakeState()
        requests = []
        expected_identity = config("bootstrap-mtls").expected_identity_uri

        class FakeTransport:
            def api_json(self, method, path, payload, expected, headers=None, certificate_request=False):
                requests.append((method, path, payload, expected, headers, certificate_request))
                return {
                    "credential_id": "credential_mtls_123",
                    "certificate": "-----BEGIN CERTIFICATE-----\nY2VydA==\n-----END CERTIFICATE-----",
                    "chain": "-----BEGIN CERTIFICATE-----\nY2hhaW4=\n-----END CERTIFICATE-----",
                    "identity_uri": expected_identity,
                    "issuer_key_id": "staging-intermediate-1",
                    "not_before": "2026-07-27T08:00:00.000Z",
                    "expires_at": "2026-07-28T08:00:00.000Z",
                    "previous_valid_until": None,
                    "already_processed": True,
                }

        lifecycle = client.NodeCertificateLifecycleClient(
            config("bootstrap-mtls"),
            state=state,
            transport=FakeTransport(),
        )
        result = lifecycle.issue_or_rotate("0.3.0-mtls-test")

        self.assertEqual(result.credential_id, "credential_mtls_123")
        self.assertTrue(result.already_processed)
        self.assertEqual(result.generation, "a" * 24)
        self.assertEqual(state.activation[2], expected_identity)
        self.assertEqual(requests[0][2]["csr"], state.pending.csr_pem)
        self.assertNotIn(state.pending.request_hash, requests[0][4]["Idempotency-Key"])
        self.assertTrue(requests[0][5])

    def test_wrong_certificate_identity_does_not_activate_pending_key(self) -> None:
        state = FakeState()

        class FakeTransport:
            def api_json(self, *args, **kwargs):
                return {
                    "credential_id": "credential_mtls_123",
                    "certificate": "certificate",
                    "chain": "chain",
                    "identity_uri": "spiffe://wavevpn/staging/tenant/other_tenant/node/other_node",
                    "issuer_key_id": "staging-intermediate-1",
                    "not_before": "2026-07-27T08:00:00.000Z",
                    "expires_at": "2026-07-28T08:00:00.000Z",
                    "previous_valid_until": None,
                    "already_processed": False,
                }

        lifecycle = client.NodeCertificateLifecycleClient(
            config("bootstrap-mtls"),
            state=state,
            transport=FakeTransport(),
        )
        with self.assertRaises(client.MtlsClientError):
            lifecycle.issue_or_rotate("0.3.0-mtls-test")
        self.assertIsNone(state.activation)

    def test_rotation_schedule_and_sanitized_metadata(self) -> None:
        expiry = datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc)
        self.assertFalse(
            client.certificate_rotation_due(
                expiry,
                6 * 60 * 60,
                now=expiry - timedelta(hours=6, seconds=1),
            )
        )
        self.assertTrue(
            client.certificate_rotation_due(
                expiry,
                6 * 60 * 60,
                now=expiry - timedelta(hours=6),
            )
        )
        result = client.CertificateLifecycleResult(
            credential_id="credential_mtls_123",
            expires_at=expiry,
            previous_valid_until=expiry - timedelta(hours=23, minutes=55),
            already_processed=False,
            generation="c" * 24,
        )
        metadata = client.sanitized_lifecycle_metadata(result)
        encoded = json.dumps(metadata)
        self.assertEqual(metadata["credential_id"], "credential_mtls_123")
        for forbidden in ("PRIVATE KEY", "BEGIN CERTIFICATE", TOKEN, "1" * 64, "2" * 64):
            self.assertNotIn(forbidden, encoded)

    def test_invalid_configuration_fails_closed(self) -> None:
        with self.assertRaises(client.MtlsClientError):
            client.MtlsClientConfig(
                api_base="http://insecure.invalid/api",
                node_id=NODE_ID,
                tenant_id=TENANT_ID,
                environment="staging",
                auth_mode="bearer",
                bearer_token=TOKEN,
            )
        with self.assertRaises(client.MtlsClientError):
            config("bootstrap-mtls", bearer=None)
        with self.assertRaises(client.MtlsClientError):
            config("unsupported")


if __name__ == "__main__":
    unittest.main()
