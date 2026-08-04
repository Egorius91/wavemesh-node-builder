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


class FakeResponse:
    def __init__(self, payload: dict[str, object], status: int = 201) -> None:
        self.status = status
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class FakeMtlsState:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.deactivated = False
        self.pending_ack = SimpleNamespace(credential_id="credential_12345678")
        self.ack_cleared = False

    def prepare_pending_request(self):
        return SimpleNamespace(public_key_hash="b" * 64)

    def pending_acknowledgement(self):
        return None if self.ack_cleared else self.pending_ack

    def clear_pending_acknowledgement(self, credential_id: str):
        if credential_id != self.pending_ack.credential_id:
            raise AssertionError("wrong acknowledgement")
        self.ack_cleared = True

    def deactivate_active_identity(self):
        self.deactivated = True
        return "c" * 24


class NodeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env_file = self.root / "agent.env"
        self.token_file = self.root / "recovery.token"
        self.tls_root = self.root / "tls"
        recovery.write_env_file(
            self.env_file,
            {
                "WAVEMESH_API_BASE": "https://api.example.invalid/api",
                "WAVEMESH_NODE_ID": NODE_ID,
                "WAVEMESH_TENANT_ID": TENANT_ID,
                "WAVEMESH_AGENT_TOKEN": OLD_TOKEN,
                "WAVEMESH_AGENT_TOKEN_EXPIRES_AT": "2026-08-01T00:00:00.000Z",
                "WAVEMESH_AGENT_MTLS_MODE": "shadow",
                "WAVEMESH_AGENT_MTLS_STATE_ROOT": str(self.tls_root),
            },
        )
        self.token_file.write_text(f"{RECOVERY_TOKEN}\n", encoding="utf-8")
        os.chmod(self.token_file, 0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def client(self) -> recovery.RecoveryClient:
        client = recovery.RecoveryClient(
            self.env_file,
            self.token_file,
            EXTERNAL_NODE_ID,
        )
        client.state = FakeMtlsState(self.tls_root)
        return client

    def response(self, **overrides):
        values: dict[str, object] = {
            "node_id": NODE_ID,
            "tenant_id": TENANT_ID,
            "auth_mode": "temporary_bearer",
            "expires_at": recovery.format_timestamp(
                datetime.now(timezone.utc) + timedelta(hours=2)
            ),
            "recovery_state": "ENROLLING",
            "already_processed": False,
        }
        values.update(overrides)
        return values

    def test_apply_generates_bearer_locally_and_never_sends_raw_bearer(self) -> None:
        captured = {}

        def open_request(req, timeout):
            captured["authorization"] = req.headers.get("Authorization")
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse(self.response())

        client = self.client()
        with mock.patch.object(recovery.request, "urlopen", side_effect=open_request):
            result = client.apply()

        values = recovery.read_env_file(self.env_file)
        new_token = values["WAVEMESH_AGENT_TOKEN"]
        self.assertTrue(recovery.valid_token(new_token))
        self.assertNotEqual(new_token, OLD_TOKEN)
        self.assertEqual(captured["authorization"], f"Bearer {RECOVERY_TOKEN}")
        self.assertEqual(captured["body"]["next_token_hash"], recovery.token_hash(new_token))
        self.assertNotIn(new_token, json.dumps(captured["body"]))
        self.assertEqual(captured["body"]["public_key_hash"], "b" * 64)
        self.assertEqual(result["mtls_runtime_state"], "BEARER_ONLY")
        self.assertTrue(result["active_generation_deactivated"])
        self.assertFalse(self.token_file.exists())
        self.assertFalse(client.pending_token_file.exists())
        self.assertFalse(client.accepted_file.exists())
        runtime = json.loads((self.tls_root / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["state"], "BEARER_ONLY")

    def test_network_failure_keeps_same_private_pending_token_for_safe_retry(self) -> None:
        client = self.client()
        with mock.patch.object(
            recovery.request,
            "urlopen",
            side_effect=recovery.error.URLError("offline"),
        ):
            with self.assertRaisesRegex(recovery.RecoveryError, "temporarily unreachable"):
                client.apply()

        first_pending = recovery.read_env_file(client.pending_token_file)[
            "WAVEMESH_PENDING_RECOVERY_AGENT_TOKEN"
        ]
        self.assertTrue(self.token_file.exists())
        self.assertFalse(client.accepted_file.exists())

        captured_hashes = []

        def successful_retry(req, timeout):
            del timeout
            captured_hashes.append(json.loads(req.data.decode("utf-8"))["next_token_hash"])
            return FakeResponse(self.response(already_processed=True))

        with mock.patch.object(recovery.request, "urlopen", side_effect=successful_retry):
            client.apply()

        values = recovery.read_env_file(self.env_file)
        self.assertEqual(values["WAVEMESH_AGENT_TOKEN"], first_pending)
        self.assertEqual(captured_hashes, [recovery.token_hash(first_pending)])

    def test_accepted_marker_finishes_locally_without_a_second_network_call(self) -> None:
        client = self.client()
        pending = recovery.generate_token()
        recovery.write_env_file(
            client.pending_token_file,
            {"WAVEMESH_PENDING_RECOVERY_AGENT_TOKEN": pending},
        )
        accepted = {
            "accepted_at": recovery.format_timestamp(datetime.now(timezone.utc)),
            "already_processed": False,
            "expires_at": recovery.format_timestamp(
                datetime.now(timezone.utc) + timedelta(hours=2)
            ),
            "next_token_hash": recovery.token_hash(pending),
            "node_id": NODE_ID,
            "tenant_id": TENANT_ID,
            "version": 1,
        }
        recovery.atomic_write_json(client.accepted_file, accepted, 0o600)

        with mock.patch.object(recovery.request, "urlopen") as urlopen:
            client.apply()
        urlopen.assert_not_called()
        self.assertEqual(
            recovery.read_env_file(self.env_file)["WAVEMESH_AGENT_TOKEN"],
            pending,
        )

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
