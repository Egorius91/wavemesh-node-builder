#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
sys.path.insert(0, str(AGENT_DIR))

import node_recovery as recovery  # noqa: E402

NODE_ID = "node_12345678"
TENANT_ID = "tenant_12345678"
EXTERNAL_NODE_ID = "ru-spb1"
OLD_TOKEN = "wvn_" + "o" * 40
RECOVERY_TOKEN = "wvr_" + "r" * 40
CSR = "-----BEGIN CERTIFICATE REQUEST-----\n" + "Q" * 96 + "\n-----END CERTIFICATE REQUEST-----\n"
REQUEST_HASH = "a" * 64
PUBLIC_KEY_HASH = "b" * 64
CREDENTIAL_ID = "credential_12345678"
CERTIFICATE = "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n"
CHAIN = "-----BEGIN CERTIFICATE-----\nCHAIN\n-----END CERTIFICATE-----\n"


class FakeResponse:
    def __init__(self, payload: dict[str, object], status: int) -> None:
        self.status = status
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount: int = -1) -> bytes:
        raw = json.dumps(self.payload).encode("utf-8")
        return raw if amount < 0 else raw[:amount]


class FakeMtlsState:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.pending_dir = root / "pending"
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.pending_key = self.pending_dir / "client.key"
        self.pending_csr = self.pending_dir / "client.csr"
        self.pending_metadata = self.pending_dir / "metadata.json"
        self.ack_path = root / "acknowledgement.pending.json"
        self.active_path = root / "fake-active.json"

    def prepare_pending_request(self):
        existing = [
            self.pending_key.exists(),
            self.pending_csr.exists(),
            self.pending_metadata.exists(),
        ]
        if any(existing) and not all(existing):
            raise RuntimeError("partial pending state")
        if not any(existing):
            self.pending_key.write_text("PRIVATE-KEY-PLACEHOLDER", encoding="utf-8")
            self.pending_csr.write_text(CSR, encoding="utf-8")
            self.pending_metadata.write_text(
                json.dumps(
                    {
                        "request_hash": REQUEST_HASH,
                        "public_key_hash": PUBLIC_KEY_HASH,
                        "created_at": "2026-08-25T12:00:00.000Z",
                    }
                ),
                encoding="utf-8",
            )
        metadata = json.loads(self.pending_metadata.read_text(encoding="utf-8"))
        return SimpleNamespace(
            csr_pem=self.pending_csr.read_text(encoding="utf-8"),
            request_hash=metadata["request_hash"],
            public_key_hash=metadata["public_key_hash"],
            created_at=metadata["created_at"],
        )

    def record_pending_acknowledgement(
        self,
        credential_id: str,
        request_hash: str,
        delivery_expires_at: datetime,
    ):
        existing = self.pending_acknowledgement()
        if existing is not None:
            if (
                existing.credential_id != credential_id
                or existing.request_hash != request_hash
                or existing.delivery_expires_at != delivery_expires_at
            ):
                raise RuntimeError("conflicting acknowledgement")
            return existing
        self.ack_path.write_text(
            json.dumps(
                {
                    "credential_id": credential_id,
                    "request_hash": request_hash,
                    "delivery_expires_at": recovery.format_timestamp(delivery_expires_at),
                }
            ),
            encoding="utf-8",
        )
        return self.pending_acknowledgement()

    def pending_acknowledgement(self):
        if not self.ack_path.exists():
            return None
        value = json.loads(self.ack_path.read_text(encoding="utf-8"))
        return SimpleNamespace(
            credential_id=value["credential_id"],
            request_hash=value["request_hash"],
            delivery_expires_at=recovery.parse_timestamp(value["delivery_expires_at"]),
        )

    def clear_pending_acknowledgement(self, credential_id: str):
        pending = self.pending_acknowledgement()
        if pending is None:
            return
        if pending.credential_id != credential_id:
            raise AssertionError("wrong acknowledgement")
        self.ack_path.unlink()

    def activate_pending_certificate(
        self,
        certificate_pem: str,
        ca_bundle_pem: str,
        expected_identity_uri: str,
    ):
        self.assert_delivery(certificate_pem, ca_bundle_pem, expected_identity_uri)
        metadata = json.loads(self.pending_metadata.read_text(encoding="utf-8"))
        self.active_path.write_text(
            json.dumps(
                {
                    "generation": "c" * 24,
                    "request_hash": metadata["request_hash"],
                    "identity_uri": expected_identity_uri,
                }
            ),
            encoding="utf-8",
        )
        self.pending_key.unlink()
        self.pending_csr.unlink()
        self.pending_metadata.unlink()
        return SimpleNamespace(generation="c" * 24)

    def assert_delivery(
        self,
        certificate_pem: str,
        ca_bundle_pem: str,
        expected_identity_uri: str,
    ) -> None:
        if certificate_pem != CERTIFICATE or ca_bundle_pem != CHAIN:
            raise AssertionError("unexpected certificate delivery")
        if not expected_identity_uri.endswith(f"/tenant/{TENANT_ID}/node/{NODE_ID}"):
            raise AssertionError("unexpected identity URI")

    def active_identity(self, expected_identity_uri: str | None = None):
        if not self.active_path.exists():
            return None
        value = json.loads(self.active_path.read_text(encoding="utf-8"))
        if expected_identity_uri is not None and value["identity_uri"] != expected_identity_uri:
            raise RuntimeError("identity mismatch")
        return SimpleNamespace(generation=value["generation"])

    def active_request_hash(self, active=None):
        del active
        if not self.active_path.exists():
            return None
        return json.loads(self.active_path.read_text(encoding="utf-8"))["request_hash"]


class NodeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env_file = self.root / "agent.env"
        self.token_file = self.root / "recovery.token"
        self.tls_root = self.root / "tls"
        self._write_env()
        self.token_file.write_text(f"{RECOVERY_TOKEN}\n", encoding="utf-8")
        os.chmod(self.token_file, 0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_env(self) -> None:
        self.env_file.write_text(
            "\n".join(
                [
                    "WAVEMESH_API_BASE=https://api.example.invalid/api",
                    f"WAVEMESH_NODE_ID={NODE_ID}",
                    f"WAVEMESH_TENANT_ID={TENANT_ID}",
                    f"WAVEMESH_AGENT_TOKEN={OLD_TOKEN}",
                    "WAVEMESH_AGENT_TOKEN_EXPIRES_AT=2026-08-01T00:00:00.000Z",
                    "WAVEMESH_AGENT_MTLS_MODE=shadow",
                    "WAVEMESH_AGENT_MTLS_ENVIRONMENT=staging",
                    f"WAVEMESH_AGENT_MTLS_STATE_ROOT={self.tls_root}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        os.chmod(self.env_file, 0o600)

    def client(self) -> recovery.RecoveryClient:
        client = recovery.RecoveryClient(
            self.env_file,
            self.token_file,
            EXTERNAL_NODE_ID,
        )
        client.state = FakeMtlsState(self.tls_root)
        return client

    def delivery(self, **overrides):
        now = datetime.now(timezone.utc)
        values: dict[str, object] = {
            "credential_id": CREDENTIAL_ID,
            "certificate": CERTIFICATE,
            "chain": CHAIN,
            "issuer_key_id": "issuer-key-12345678",
            "lifecycle_status": "PENDING_ACKNOWLEDGEMENT",
            "recovery_reason": "LOST_KEY",
            "not_before": recovery.format_timestamp(now - timedelta(minutes=1)),
            "expires_at": recovery.format_timestamp(now + timedelta(hours=24)),
            "delivery_expires_at": recovery.format_timestamp(now + timedelta(minutes=15)),
            "previous_valid_until": None,
            "already_processed": False,
        }
        values.update(overrides)
        return values

    def acknowledgement(self, **overrides):
        values: dict[str, object] = {
            "credential_id": CREDENTIAL_ID,
            "lifecycle_status": "ACKNOWLEDGED",
            "acknowledged_at": recovery.format_timestamp(datetime.now(timezone.utc)),
            "already_processed": False,
        }
        values.update(overrides)
        return values

    def test_apply_posts_exact_csr_then_acknowledges_without_bearer_mutation(self) -> None:
        captured: list[dict[str, object]] = []

        def open_request(req, timeout):
            captured.append(
                {
                    "method": req.method,
                    "url": req.full_url,
                    "authorization": req.headers.get("Authorization"),
                    "body": json.loads(req.data.decode("utf-8")) if req.data else None,
                    "timeout": timeout,
                }
            )
            if req.full_url.endswith("/recover/certificates"):
                return FakeResponse(self.delivery(), 201)
            if req.full_url.endswith(f"/{CREDENTIAL_ID}/acknowledge"):
                return FakeResponse(self.acknowledgement(), 201)
            raise AssertionError(f"unexpected URL: {req.full_url}")

        client = self.client()
        with mock.patch.object(recovery.request, "urlopen", side_effect=open_request):
            result = client.apply()

        self.assertEqual(
            captured[0]["body"],
            {"tenant_id": TENANT_ID, "node_id": NODE_ID, "csr": CSR},
        )
        self.assertEqual(captured[0]["method"], "POST")
        self.assertEqual(captured[1]["method"], "POST")
        self.assertEqual(captured[1]["body"], {})
        self.assertTrue(
            all(item["authorization"] == f"Bearer {RECOVERY_TOKEN}" for item in captured)
        )
        self.assertIn(f"WAVEMESH_AGENT_TOKEN={OLD_TOKEN}", self.env_file.read_text(encoding="utf-8"))
        self.assertFalse(self.token_file.exists())
        self.assertFalse(client.pending_file.exists())
        self.assertIsNone(client.state.pending_acknowledgement())
        runtime = json.loads((self.tls_root / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["state"], "SHADOW_READY")
        rendered = json.dumps(result)
        self.assertNotIn(RECOVERY_TOKEN, rendered)
        self.assertNotIn(CSR, rendered)
        self.assertNotIn(CERTIFICATE, rendered)
        self.assertNotIn(CHAIN, rendered)
        self.assertEqual(result["credential_id"], CREDENTIAL_ID)

    def test_ambiguous_post_keeps_same_csr_and_marker_for_retry(self) -> None:
        first = self.client()
        with mock.patch.object(
            recovery.request,
            "urlopen",
            side_effect=recovery.error.URLError("offline"),
        ):
            with self.assertRaisesRegex(recovery.RecoveryError, "temporarily unreachable"):
                first.apply()

        marker = json.loads(first.pending_file.read_text(encoding="utf-8"))
        first_csr = first.state.pending_csr.read_text(encoding="utf-8")
        self.assertEqual(marker["request_hash"], REQUEST_HASH)
        self.assertTrue(self.token_file.exists())

        captured_bodies: list[dict[str, object]] = []

        def retry(req, timeout):
            del timeout
            if req.full_url.endswith("/recover/certificates"):
                body = json.loads(req.data.decode("utf-8"))
                captured_bodies.append(body)
                return FakeResponse(self.delivery(already_processed=True), 201)
            if req.full_url.endswith(f"/{CREDENTIAL_ID}/acknowledge"):
                return FakeResponse(self.acknowledgement(), 201)
            raise AssertionError("unexpected recovery URL")

        restarted = self.client()
        with mock.patch.object(recovery.request, "urlopen", side_effect=retry):
            restarted.apply()

        self.assertEqual(captured_bodies, [{"tenant_id": TENANT_ID, "node_id": NODE_ID, "csr": first_csr}])
        self.assertEqual(first_csr, CSR)

    def test_restart_after_delivery_binding_retrieves_same_credential_then_acks(self) -> None:
        first = self.client()

        def crash_before_activation(*_args, **_kwargs):
            raise RuntimeError("simulated crash")

        first.state.activate_pending_certificate = crash_before_activation
        with mock.patch.object(
            recovery.request,
            "urlopen",
            return_value=FakeResponse(self.delivery(), 201),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                first.apply()

        marker = json.loads(first.pending_file.read_text(encoding="utf-8"))
        self.assertEqual(marker["credential_id"], CREDENTIAL_ID)
        self.assertIsNotNone(first.state.pending_acknowledgement())
        self.assertTrue(first.state.pending_csr.exists())

        calls: list[tuple[str, str]] = []

        def resume(req, timeout):
            del timeout
            calls.append((req.method, req.full_url))
            if req.method == "GET" and req.full_url.endswith(
                f"/recover/certificates/{CREDENTIAL_ID}"
            ):
                return FakeResponse(self.delivery(already_processed=True), 200)
            if req.method == "POST" and req.full_url.endswith(
                f"/{CREDENTIAL_ID}/acknowledge"
            ):
                return FakeResponse(self.acknowledgement(), 201)
            raise AssertionError("resume attempted a new certificate request")

        restarted = self.client()
        with mock.patch.object(recovery.request, "urlopen", side_effect=resume):
            restarted.apply()

        self.assertEqual([method for method, _ in calls], ["GET", "POST"])
        self.assertFalse(self.token_file.exists())

    def test_lost_ack_response_retries_only_same_ack_after_restart(self) -> None:
        first = self.client()
        calls = 0

        def first_run(req, timeout):
            nonlocal calls
            del timeout
            calls += 1
            if req.full_url.endswith("/recover/certificates"):
                return FakeResponse(self.delivery(), 201)
            if req.full_url.endswith(f"/{CREDENTIAL_ID}/acknowledge"):
                raise recovery.error.URLError("ack response lost")
            raise AssertionError("unexpected URL")

        with mock.patch.object(recovery.request, "urlopen", side_effect=first_run):
            with self.assertRaisesRegex(recovery.RecoveryError, "temporarily unreachable"):
                first.apply()

        self.assertEqual(calls, 2)
        self.assertFalse(first.state.pending_csr.exists())
        self.assertIsNotNone(first.state.pending_acknowledgement())
        self.assertTrue(first.pending_file.exists())
        self.assertTrue(self.token_file.exists())
        self.assertIsNotNone(first.state.active_identity(first.expected_identity_uri))

        resumed_calls: list[str] = []

        def retry_ack(req, timeout):
            del timeout
            resumed_calls.append(req.full_url)
            if req.full_url.endswith(f"/{CREDENTIAL_ID}/acknowledge"):
                return FakeResponse(
                    self.acknowledgement(already_processed=True),
                    200,
                )
            raise AssertionError("retry attempted certificate issuance or retrieval")

        restarted = self.client()
        with mock.patch.object(recovery.request, "urlopen", side_effect=retry_ack):
            result = restarted.apply()

        self.assertEqual(len(resumed_calls), 1)
        self.assertTrue(resumed_calls[0].endswith(f"/{CREDENTIAL_ID}/acknowledge"))
        self.assertTrue(result["acknowledgement_replayed"])
        self.assertFalse(self.token_file.exists())
        self.assertFalse(restarted.pending_file.exists())

    def test_marker_request_hash_conflict_fails_before_network(self) -> None:
        client = self.client()
        with mock.patch.object(
            recovery.request,
            "urlopen",
            side_effect=recovery.error.URLError("offline"),
        ):
            with self.assertRaises(recovery.RecoveryError):
                client.apply()

        marker = json.loads(client.pending_file.read_text(encoding="utf-8"))
        marker["request_hash"] = "f" * 64
        recovery.atomic_write_json(client.pending_file, marker, 0o600)

        restarted = self.client()
        with mock.patch.object(recovery.request, "urlopen") as urlopen:
            with self.assertRaisesRegex(
                recovery.RecoveryError,
                "does not match the pending CSR/private key",
            ):
                restarted.apply()
        urlopen.assert_not_called()

    def test_legacy_temporary_bearer_marker_fails_closed(self) -> None:
        client = self.client()
        client.legacy_accepted_file.write_text('{"version":1}', encoding="utf-8")
        os.chmod(client.legacy_accepted_file, 0o600)
        with self.assertRaisesRegex(recovery.RecoveryError, "Legacy temporary-bearer"):
            client.check()

    def test_recovery_token_requires_exact_private_permissions(self) -> None:
        unsafe_metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o640)
        with mock.patch.object(Path, "lstat", return_value=unsafe_metadata):
            with self.assertRaisesRegex(recovery.RecoveryError, "permissions must be 0600"):
                recovery.read_restricted_token(
                    self.token_file,
                    recovery.RECOVERY_TOKEN_PATTERN,
                    "Recovery token",
                )


if __name__ == "__main__":
    unittest.main()
