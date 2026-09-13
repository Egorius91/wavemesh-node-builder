#!/usr/bin/env python3
"""Real local certificate/state recovery across processes; issuer transport is fake.

No staging endpoint, network request, or live credential is used.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
import node_mtls_client as client
import node_mtls_runtime as runtime
from node_mtls_state import NodeMtlsState

OPENSSL = os.environ.get("OPENSSL_BINARY") or shutil.which("openssl")
IDENTITY = "spiffe://wavevpn/staging/tenant/tenant_12345678/node/node_12345678"


def issue(root, csr, prefix):
    helper = runpy.run_path(str(ROOT / "tests/unit/test_node_mtls_state.py"))
    return helper["issue_test_certificate"](root, csr, IDENTITY, prefix)


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def worker(root, timestamp, fault):
    now = datetime.fromisoformat(timestamp)
    state = NodeMtlsState(root / "tls", openssl_binary=OPENSSL)
    ledger_path = root / "issuer.json"
    ledger = json.loads(ledger_path.read_text())

    class Issuer:
        def api_json(self, method, path, payload, expected, headers=None, **kwargs):
            assert method == "POST"
            if path.endswith("/acknowledge"):
                ledger["ack_calls"] += 1
                ledger["acknowledged"] = True
                save(ledger_path, ledger)
                if fault == "lost_ack":
                    raise client.MtlsApiError(0, "NETWORK_OR_TLS_ERROR", True)
                return {
                    "credential_id": "credential_process_123",
                    "lifecycle_status": "ACKNOWLEDGED",
                    "acknowledged_at": now.isoformat(),
                    "already_processed": ledger["ack_calls"] > 1,
                }
            assert path.endswith("/certificates")
            ledger["issue_calls"] += 1
            request = {
                "csr_hash": hashlib.sha256(payload["csr"].encode()).hexdigest(),
                "idempotency_key": headers["Idempotency-Key"],
            }
            if "request" in ledger:
                assert ledger["request"] == request, "renewal request changed"
            ledger["request"] = request
            save(ledger_path, ledger)
            if fault == "outage":
                raise client.MtlsApiError(503, "ISSUER_UNAVAILABLE", True)
            if "delivery" not in ledger:
                certificate, ca = issue(root, state.pending_csr, "renewed")
                ledger["sign_calls"] += 1
                ledger["delivery"] = {
                    "credential_id": "credential_process_123",
                    "certificate": certificate.read_text(),
                    "chain": ca.read_text(),
                    "expires_at": (now + timedelta(hours=20)).isoformat(),
                    "delivery_expires_at": (now + timedelta(minutes=15)).isoformat(),
                    "previous_valid_until": None,
                    "lifecycle_status": "PENDING_ACKNOWLEDGEMENT",
                    "already_processed": False,
                }
                save(ledger_path, ledger)
            if fault == "lost_issue":
                raise client.MtlsApiError(0, "NETWORK_OR_TLS_ERROR", True)
            return {**ledger["delivery"], "already_processed": True}

    actual_client = client.NodeCertificateLifecycleClient

    def factory(config, state=None):
        assert config.auth_mode == "mtls" and config.bearer_token is None
        return actual_client(config, state=state, transport=Issuer())

    config = runtime.MtlsRuntimeConfig(
        mode="shadow", bearer_api_base=None, bearer_token=None,
        bearer_bootstrap_enabled=False,
        mtls_api_base="https://issuer.example.invalid/api",
        node_id="node_12345678", tenant_id="tenant_12345678",
        environment="staging", state_root=root / "tls",
        rotate_before_seconds=3 * 86400, retry_base_seconds=5,
        retry_max_seconds=30, retry_max_attempts=3,
    )
    instance = runtime.NodeMtlsRuntime(config, state=state)
    with mock.patch.object(runtime, "NodeCertificateLifecycleClient", side_effect=factory):
        status = instance.lifecycle_cycle("0.6.0-process-test", now)
    print(json.dumps({"state": status.state.value, "attempts": status.retry_attempts}))


@unittest.skipUnless(OPENSSL, "OpenSSL is required")
@unittest.skipIf(os.name == "nt", "Real generation symlinks require Linux CI")
class RenewalProcessTests(unittest.TestCase):
    def test_outage_and_lost_responses_recover_one_identity_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = NodeMtlsState(root / "tls", openssl_binary=OPENSSL)
            state.prepare_pending_request()
            certificate, ca = issue(root, state.pending_csr, "initial")
            old = state.activate_pending_certificate(
                certificate.read_text(), ca.read_text(), IDENTITY,
            )
            save(root / "issuer.json", {
                "issue_calls": 0, "ack_calls": 0, "sign_calls": 0,
            })
            now = datetime.now(timezone.utc)

            def cycle(seconds, fault):
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--worker",
                     str(root), (now + timedelta(seconds=seconds)).isoformat(), fault],
                    capture_output=True, text=True, timeout=45,
                    env={**os.environ, "OPENSSL_BINARY": str(OPENSSL)},
                )
                self.assertEqual(result.returncode, 0, "worker failed; raw output suppressed")
                return json.loads(result.stdout)

            def ledger():
                return json.loads((root / "issuer.json").read_text())

            self.assertEqual(cycle(0, "outage")["attempts"], 1)
            pending = tuple(p.read_bytes() for p in (
                state.pending_key, state.pending_csr, state.pending_metadata,
            ))
            cycle(5, "outage")
            self.assertEqual(cycle(15, "outage"), {"state": "FALLBACK", "attempts": 3})
            cycle(44, "healthy")
            self.assertEqual(ledger()["issue_calls"], 3)
            self.assertEqual(cycle(45, "lost_issue")["state"], "FALLBACK")
            self.assertEqual(pending, tuple(p.read_bytes() for p in (
                state.pending_key, state.pending_csr, state.pending_metadata,
            )))
            self.assertEqual(state.active_identity(IDENTITY).generation, old.generation)
            self.assertEqual(cycle(75, "lost_ack")["state"], "FALLBACK")
            self.assertIsNotNone(state.pending_acknowledgement())
            self.assertFalse(state.pending_key.exists())
            cycle(104, "healthy")
            self.assertEqual(ledger()["ack_calls"], 1)
            self.assertEqual(cycle(105, "healthy"), {"state": "SHADOW_READY", "attempts": 0})
            self.assertIsNone(state.pending_acknowledgement())
            self.assertNotEqual(state.active_identity(IDENTITY).generation, old.generation)
            self.assertEqual(ledger()["sign_calls"], 1)
            self.assertEqual(ledger()["issue_calls"], 5)
            self.assertEqual(ledger()["ack_calls"], 2)
            self.assertTrue(ledger()["acknowledged"])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    else:
        unittest.main()
