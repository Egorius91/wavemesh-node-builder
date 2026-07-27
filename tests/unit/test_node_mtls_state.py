#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "agent" / "node_mtls_state.py"
SPEC = importlib.util.spec_from_file_location("wave_node_mtls_state", MODULE_PATH)
assert SPEC and SPEC.loader
mtls = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mtls
SPEC.loader.exec_module(mtls)

OPENSSL = shutil.which("openssl")
IDENTITY = "spiffe://wavevpn/staging/tenant/tenant_12345678/node/node_12345678"


@unittest.skipUnless(OPENSSL, "OpenSSL is required")
class NodeMtlsStateTests(unittest.TestCase):
    def test_pending_request_is_private_idempotent_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = mtls.NodeMtlsState(Path(directory) / "tls", openssl_binary=OPENSSL)
            first = state.prepare_pending_request()
            key_before = state.pending_key.read_bytes()
            second = state.prepare_pending_request()
            metadata_text = state.pending_metadata.read_text(encoding="utf-8")

            self.assertEqual(first, second)
            self.assertEqual(state.pending_key.read_bytes(), key_before)
            self.assertEqual(state.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(state.pending_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(state.pending_key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(state.pending_csr.stat().st_mode & 0o777, 0o600)
            self.assertEqual(state.pending_metadata.stat().st_mode & 0o777, 0o600)
            self.assertRegex(first.request_hash, r"^[a-f0-9]{64}$")
            self.assertRegex(first.public_key_hash, r"^[a-f0-9]{64}$")
            self.assertNotIn("PRIVATE KEY", metadata_text)
            self.assertNotIn(first.csr_pem, metadata_text)
            self.assertNotIn(key_before.decode("utf-8"), metadata_text)

    def test_partial_pending_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = mtls.NodeMtlsState(Path(directory) / "tls", openssl_binary=OPENSSL)
            state.pending_dir.mkdir(parents=True, mode=0o700)
            state.pending_key.write_text("partial", encoding="utf-8")
            os.chmod(state.pending_key, 0o600)

            with self.assertRaises(mtls.MtlsStateError):
                state.prepare_pending_request()

    def test_activate_certificate_uses_atomic_generation_and_clears_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = mtls.NodeMtlsState(root / "tls", openssl_binary=OPENSSL)
            pending = state.prepare_pending_request()
            certificate, ca = issue_test_certificate(root, state.pending_csr, IDENTITY)

            active = state.activate_pending_certificate(
                certificate.read_text(encoding="utf-8"),
                ca.read_text(encoding="utf-8"),
                IDENTITY,
            )
            reloaded = state.active_identity()
            metadata = json.loads(active.metadata.read_text(encoding="utf-8"))

            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.generation, active.generation)
            self.assertTrue(state.active_link.is_symlink())
            self.assertEqual(active.private_key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(active.certificate.stat().st_mode & 0o777, 0o644)
            self.assertEqual(active.ca_bundle.stat().st_mode & 0o777, 0o644)
            self.assertEqual(active.metadata.stat().st_mode & 0o777, 0o600)
            self.assertFalse(state.pending_key.exists())
            self.assertFalse(state.pending_csr.exists())
            self.assertFalse(state.pending_metadata.exists())
            self.assertEqual(metadata["request_hash"], pending.request_hash)
            self.assertEqual(metadata["public_key_hash"], pending.public_key_hash)
            self.assertNotIn(IDENTITY, json.dumps(metadata))
            self.assertEqual(metadata["identity_uri_hash"], mtls.sha256_text(IDENTITY))

    def test_wrong_identity_is_rejected_and_pending_state_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = mtls.NodeMtlsState(root / "tls", openssl_binary=OPENSSL)
            state.prepare_pending_request()
            certificate, ca = issue_test_certificate(root, state.pending_csr, IDENTITY)

            with self.assertRaises(mtls.MtlsStateError):
                state.activate_pending_certificate(
                    certificate.read_text(encoding="utf-8"),
                    ca.read_text(encoding="utf-8"),
                    "spiffe://wavevpn/staging/tenant/tenant_12345678/node/node_other123",
                )

            self.assertTrue(state.pending_key.exists())
            self.assertTrue(state.pending_csr.exists())
            self.assertTrue(state.pending_metadata.exists())
            self.assertIsNone(state.active_identity())

    def test_certificate_for_another_private_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = mtls.NodeMtlsState(root / "tls", openssl_binary=OPENSSL)
            state.prepare_pending_request()
            other_key = root / "other.key"
            other_csr = root / "other.csr"
            run_openssl(
                [
                    "genpkey",
                    "-algorithm",
                    "EC",
                    "-pkeyopt",
                    "ec_paramgen_curve:P-256",
                    "-out",
                    str(other_key),
                ]
            )
            run_openssl(
                [
                    "req",
                    "-new",
                    "-sha256",
                    "-key",
                    str(other_key),
                    "-subj",
                    "/CN=Other Node",
                    "-out",
                    str(other_csr),
                ]
            )
            certificate, ca = issue_test_certificate(root, other_csr, IDENTITY, prefix="other")

            with self.assertRaises(mtls.MtlsStateError):
                state.activate_pending_certificate(
                    certificate.read_text(encoding="utf-8"),
                    ca.read_text(encoding="utf-8"),
                    IDENTITY,
                )

            self.assertTrue(state.pending_key.exists())
            self.assertIsNone(state.active_identity())

    def test_active_symlink_outside_generations_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = mtls.NodeMtlsState(root / "tls", openssl_binary=OPENSSL)
            state.prepare_pending_request()
            state.clear_pending_request()
            outside = root / "outside"
            outside.mkdir()
            os.symlink(str(outside), state.active_link)

            with self.assertRaises(mtls.MtlsStateError):
                state.active_identity()


def issue_test_certificate(
    directory: Path,
    csr: Path,
    identity: str,
    prefix: str = "test",
) -> tuple[Path, Path]:
    ca_key = directory / f"{prefix}-ca.key"
    ca_certificate = directory / f"{prefix}-ca.crt"
    certificate = directory / f"{prefix}-client.crt"
    extensions = directory / f"{prefix}-extensions.cnf"
    extensions.write_text(
        "\n".join(
            [
                "basicConstraints=critical,CA:FALSE",
                "keyUsage=critical,digitalSignature",
                "extendedKeyUsage=clientAuth",
                f"subjectAltName=URI:{identity}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    run_openssl(
        [
            "genpkey",
            "-algorithm",
            "EC",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-out",
            str(ca_key),
        ]
    )
    run_openssl(
        [
            "req",
            "-x509",
            "-new",
            "-sha256",
            "-key",
            str(ca_key),
            "-subj",
            "/CN=WaveMesh Unit Test CA",
            "-days",
            "1",
            "-out",
            str(ca_certificate),
        ]
    )
    run_openssl(
        [
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(ca_certificate),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(extensions),
            "-out",
            str(certificate),
        ]
    )
    return certificate, ca_certificate


def run_openssl(arguments: list[str]) -> None:
    completed = subprocess.run(
        [OPENSSL, *arguments],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
