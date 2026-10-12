#!/usr/bin/env python3
"""Native Linux PKI/ownership proof plus fixed-capability contract negatives."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from uuid import uuid4
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
import node_expired_identity as expired
import expired_node_helper as helper
import node_mtls_state as mtls
from node_recovery import RecoveryError
from test_node_mtls_state import issue_test_certificate, run_openssl, OPENSSL, IDENTITY


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now(timezone.utc).isoformat()
        self.config = {"enabled": False, "compromised": False, "externalId": "entry-fixed",
                       "hostBindingHash": "a" * 64, "nodeIdentityHash": "b" * 64, "trustedCaBundleSha256": "f" * 64}
        self.binding = {"schemaVersion": 1, "operationId": str(uuid4()), "hostBindingHash": "a" * 64,
                        "nodeIdentityHash": "b" * 64, "expiredCredentialId": "expired_12345678",
                        "certificateFingerprintHash": "c" * 64, "certificateSerialHash": "d" * 64,
                        "publicKeyHash": "e" * 64, "trustedCaBundleSha256": "f" * 64,
                        "certificateExpiresAt": "2026-10-11T00:00:00Z", "baselineObservedAt": now,
                        "acknowledged": True, "revoked": False, "quarantined": False, "compromised": False}

    def test_fixed_commands_reject_paths_environment_and_shell(self):
        for command in ("sh", "inspect;id", "apply /tmp/key", "WAVEMESH_AGENT_ENV=/tmp/x apply", "wrapper-admission"):
            with mock.patch.object(helper, "load_config") as loader:
                with self.assertRaises(mtls.MtlsStateError):
                    helper.dispatch(command, {})
                loader.assert_not_called()

    def test_duplicate_json_fields_rejected_even_inside_nested_binding(self):
        for raw in ('{"operationId":"first","operationId":"second"}', '{"binding":{"token":"first","token":"second"}}'):
            with self.assertRaisesRegex(mtls.MtlsStateError, "duplicate fields"):
                json.loads(raw, object_pairs_hook=mtls.unique_json_object)

    def test_immutable_config_attestation_survives_only_enabled_toggle(self):
        config = {**self.config, "sourceHashes": {"expired_node_helper.py": "1" * 64}}
        enabled = helper.attestation({**config, "enabled": True})
        disabled = helper.attestation({**config, "enabled": False})
        self.assertEqual(enabled, disabled)
        for field, changed in (("compromised", True), ("trustedCaBundleSha256", "0" * 64), ("expiresAt", "2030-01-01T00:00:00Z")):
            self.assertNotEqual(enabled["configSha256"], helper.attestation({**config, field: changed})["configSha256"])

    def test_binding_requires_exact_hash_and_canonical_v4_uuid(self):
        config = {"hostBindingHash": "a" * 64, "nodeIdentityHash": "b" * 64, "trustedCaBundleSha256": "f" * 64}
        data = {**config, "schemaVersion": 1, "operationId": str(uuid4()),
                "expiredCredentialId": "expired_12345678",
                "certificateFingerprintHash": "c" * 64, "certificateSerialHash": "d" * 64,
                "publicKeyHash": "e" * 64, "trustedCaBundleSha256": "f" * 64,
                "certificateExpiresAt": "2026-10-11T00:00:00Z"}
        helper.validate_binding(config, data)
        for override in ({"hostBindingHash": "0" * 64}, {"schemaVersion": 2}, {"operationId": data["operationId"].upper()}):
            with self.assertRaises((mtls.MtlsStateError, ValueError)):
                helper.validate_binding(config, {**data, **override})

    def test_forced_command_clears_client_environment_and_bounds_commands(self):
        script = (ROOT / "agent" / "expired-node-forced-command.sh").read_text()
        self.assertIn("/usr/bin/env -i", script)
        self.assertIn("inspect|prepare|deliver|apply|reconcile", script)
        self.assertNotIn("eval", script)

    def test_unknown_apply_is_durable_and_never_dispatches_wrapper_twice(self):
        current = {"binding": self.binding, "tokenHash": "f" * 64, "phase": "DELIVERED"}
        client = mock.Mock()
        client._load_marker.return_value = {"credential_id": None}
        with mock.patch.object(helper, "load_config", return_value=self.config), \
             mock.patch.object(helper, "journal", return_value=current), \
             mock.patch.object(helper, "admission"), \
             mock.patch.object(helper, "RecoveryClient", return_value=client), \
             mock.patch.object(helper, "atomic_write_json") as persist, \
             mock.patch.object(helper.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr=b"")) as execute:
            result = helper.dispatch("apply", self.binding)
            self.assertEqual(result["code"], "SIGN_OUTCOME_UNKNOWN")
            self.assertEqual(current["phase"], "APPLY_DISPATCHED")
            persist.assert_called_once()
            with self.assertRaisesRegex(mtls.MtlsStateError, "requires reconciliation"):
                helper.dispatch("apply", self.binding)
            execute.assert_called_once()

    def test_reconcile_without_receipt_cannot_launch_or_issue(self):
        current = {"binding": self.binding, "tokenHash": "f" * 64, "phase": "APPLY_DISPATCHED"}
        client = mock.Mock()
        client._load_marker.return_value = None
        client.completed_file = Path("does-not-exist")
        with mock.patch.object(helper, "load_config", return_value=self.config), \
             mock.patch.object(helper, "journal", return_value=current), \
             mock.patch.object(helper, "RecoveryClient", return_value=client), \
             mock.patch.object(helper.subprocess, "run") as execute:
            with self.assertRaisesRegex(mtls.MtlsStateError, "Lost local receipt"):
                helper.dispatch("reconcile", self.binding)
            execute.assert_not_called()

    def test_disabled_pilot_allows_known_receipt_reconciliation_only(self):
        current = {"binding": self.binding, "tokenHash": "f" * 64, "phase": "APPLY_DISPATCHED"}
        client = mock.Mock()
        client._load_marker.return_value = {"credential_id": "known_12345678", "operation_id": self.binding["operationId"]}
        with mock.patch.object(helper, "load_config", return_value=self.config), \
             mock.patch.object(helper, "journal", return_value=current), \
             mock.patch.object(helper, "RecoveryClient", return_value=client), \
             mock.patch.object(helper, "atomic_write_json"), \
             mock.patch.object(helper, "admission", side_effect=AssertionError("fresh admission forbidden")), \
             mock.patch.object(helper.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr=b"")) as execute:
            result = helper.dispatch("reconcile", self.binding)
            self.assertEqual(result["code"], "KNOWN_CREDENTIAL_RECONCILE")
            self.assertTrue(result["retry_safe"])
            execute.assert_called_once()

    def no_sign_fixture(self):
        pem = "-----BEGIN CERTIFICATE REQUEST-----\nRml4dHVyZUNzclBlbQ==\n-----END CERTIFICATE REQUEST-----\n"
        pending = SimpleNamespace(csr_pem=pem, request_hash="1" * 64, public_key_hash="2" * 64)
        proof = {"schema_version": 1, "receipt_id": str(uuid4()), "operation_id": self.binding["operationId"],
                 "authorization_id": "authorization_12345678", "pem_request_hash": hashlib.sha256(mtls.canonical_csr_pem(pem).encode()).hexdigest(),
                 "local_request_hash": pending.request_hash, "pending_public_key_hash": pending.public_key_hash,
                 "observed_at": datetime.now(timezone.utc).isoformat()}
        marker = {"operation_id": self.binding["operationId"], "dispatch_state": "KNOWN_NO_SIGN", "credential_id": None,
                  "request_hash": pending.request_hash, "public_key_hash": pending.public_key_hash,
                  "post_dispatched_at": datetime.now(timezone.utc).isoformat(), "no_sign_proof": proof}
        client = mock.Mock()
        client._load_marker.side_effect = lambda **_: marker
        client.state._load_and_validate_pending.return_value = pending
        def write(value):
            marker.clear()
            marker.update(value)
        client._write_marker.side_effect = write
        return client, marker, proof

    def test_exact_local_and_db_no_sign_receipts_allow_only_same_request_continuation(self):
        current = {"binding": self.binding, "phase": "APPLY_DISPATCHED", "tokenHash": "f" * 64}
        client, marker, proof = self.no_sign_fixture()
        payload = {**self.binding, "noSignProof": proof, "newSignAllowed": True}
        def dispatched(_args, **_kwargs):
            self.assertEqual(marker["dispatch_state"], "READY")
            self.assertIsNone(marker["post_dispatched_at"])
            # A subsequent unknown POST immediately becomes unknown again;
            # the previous no-SIGN receipt cannot downgrade that new dispatch.
            marker["post_dispatched_at"] = datetime.now(timezone.utc).isoformat()
            return SimpleNamespace(returncode=1, stderr=b"")
        with mock.patch.object(helper, "load_config", return_value=self.config), \
             mock.patch.object(helper, "journal", return_value=current), \
             mock.patch.object(helper, "RecoveryClient", return_value=client), \
             mock.patch.object(helper, "atomic_write_json"), mock.patch.object(helper, "admission") as admission, \
             mock.patch.object(helper.subprocess, "run", side_effect=dispatched) as execute:
            result = helper.dispatch("reconcile", payload)
            self.assertEqual(result["code"], "SIGN_OUTCOME_UNKNOWN")
            admission.assert_called_once_with(self.config, payload, known_no_sign=True)
            with self.assertRaises(RecoveryError):
                helper.dispatch("reconcile", payload)
            execute.assert_called_once()

    def test_no_sign_db_mismatch_or_unknown_local_dispatch_cannot_clear_guard(self):
        current = {"binding": self.binding, "phase": "APPLY_DISPATCHED", "tokenHash": "f" * 64}
        client, marker, proof = self.no_sign_fixture()
        with mock.patch.object(helper, "load_config", return_value=self.config), \
             mock.patch.object(helper, "journal", return_value=current), \
             mock.patch.object(helper, "RecoveryClient", return_value=client), \
             mock.patch.object(helper.subprocess, "run") as execute:
            with self.assertRaisesRegex(mtls.MtlsStateError, "Independent no-SIGN"):
                helper.dispatch("reconcile", {**self.binding, "newSignAllowed": True, "noSignProof": {**proof, "receipt_id": str(uuid4())}})
            self.assertEqual(marker["dispatch_state"], "KNOWN_NO_SIGN")
            marker["dispatch_state"] = "READY"
            marker.pop("no_sign_proof")
            with self.assertRaises(RecoveryError):
                helper.dispatch("reconcile", {**self.binding, "newSignAllowed": True, "noSignProof": proof})
            execute.assert_not_called()

    @unittest.skipUnless(os.name == "nt" or os.geteuid() == 0, "Private custody files require Linux root")
    def test_lost_delivery_resumes_same_token_bytes_and_rejects_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path, journal_path, config_path = (root / name for name in ("token", "operation.json", "config.json"))
            mtls.atomic_write_json(config_path, self.config, 0o600)
            config = {**self.config, "sourceHashes": {"expired_node_helper.py": "1" * 64}}
            token = "wvr_" + "r" * 40
            payload = {**self.binding, "token": token}
            with mock.patch.object(helper, "TOKEN", token_path), mock.patch.object(helper, "JOURNAL", journal_path), \
                 mock.patch.object(helper, "CONFIG", config_path), mock.patch.object(helper, "load_config", return_value=config), \
                 mock.patch.object(helper, "admission"):
                with mock.patch.object(helper, "atomic_write_bytes", side_effect=OSError("simulated token delivery loss")):
                    with self.assertRaises(OSError):
                        helper.dispatch("deliver", payload)
                self.assertTrue(journal_path.exists())
                self.assertFalse(token_path.exists())
                result = helper.dispatch("deliver", payload)
                self.assertEqual(token_path.read_bytes(), token.encode() + b"\n")
                self.assertNotIn(token, json.dumps(result))
                helper.dispatch("deliver", payload)
                with self.assertRaisesRegex(mtls.MtlsStateError, "replacement forbidden"):
                    helper.dispatch("deliver", {**payload, "token": "wvr_" + "s" * 40})
                self.assertEqual(token_path.read_bytes(), token.encode() + b"\n")

    @unittest.skipUnless(os.name == "nt" or os.geteuid() == 0, "Private custody files require Linux root")
    def test_accepted_operation_archived_before_new_incident_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "operations"
            history.mkdir(mode=0o700)
            config_path, journal_path, token_path = (root / name for name in ("config.json", "operation.json", "token"))
            config = {**self.config, "sourceHashes": {"expired_node_helper.py": "1" * 64}}
            mtls.atomic_write_json(config_path, config, 0o600)
            accepted = {"binding": self.binding, "tokenHash": "7" * 64, "phase": "ACCEPTED", "activeRequestHash": "8" * 64}
            mtls.atomic_write_json(journal_path, accepted, 0o600)
            new_binding = {**self.binding, "operationId": str(uuid4()), "expiredCredentialId": "new_expired_12345678", "certificateFingerprintHash": "9" * 64}
            with mock.patch.object(helper, "TOKEN", token_path), mock.patch.object(helper, "JOURNAL", journal_path), \
                 mock.patch.object(helper, "HISTORY", history), mock.patch.object(helper, "CONFIG", config_path), \
                 mock.patch.object(helper, "load_config", return_value=config), mock.patch.object(helper, "admission"):
                helper.dispatch("deliver", {**new_binding, "token": "wvr_" + "r" * 40})
            archived = mtls.read_json_object(history / (self.binding["operationId"] + ".json"))
            self.assertEqual(archived, accepted)
            self.assertEqual(mtls.read_json_object(journal_path)["binding"], new_binding)

    @unittest.skipUnless(os.name == "nt" or os.geteuid() == 0, "Private custody files require Linux root")
    def test_cleanup_removes_only_same_operation_backup_tokens_after_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backups = root / "backups"
            backups.mkdir(mode=0o700)
            operation = self.binding["operationId"]
            own, unrelated = backups / "own", backups / "unrelated"
            for path, op in ((own, operation), (unrelated, str(uuid4()))):
                path.mkdir(mode=0o700)
                mtls.atomic_write_bytes(path / "pilot.operation.id", (op + "\n").encode(), 0o600)
                mtls.atomic_write_bytes(path / "recovery.token.before", b"PRIVATE_TOKEN_FIXTURE", 0o600)
                mtls.atomic_write_bytes(path / "SHA256SUMS", b"previous-checksum-receipt\n", 0o600)
            completed, runtime = root / "completed.json", root / "runtime.json"
            mtls.atomic_write_json(completed, {"operation_id": operation, "acknowledged_at": "2026-10-12T01:00:00Z",
                                             "credential_id": "known_12345678", "request_hash": "a" * 64}, 0o600)
            mtls.atomic_write_json(runtime, {"state": "SHADOW_ACTIVE"}, 0o600)
            client = mock.Mock()
            client.completed_file, client.runtime_file, client.pending_file = completed, runtime, root / "absent.pending"
            client.state.acknowledgement_path = root / "absent.ack"
            client.state.active_request_hash.return_value = "a" * 64
            with mock.patch.object(helper, "TOKEN", root / "absent.token"), mock.patch.object(helper, "BACKUPS", backups), \
                 mock.patch.object(helper.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
                proof = helper.cleanup_evidence(client, operation, remove_backup_tokens=True)
            self.assertTrue(proof["backup_token_absent"])
            self.assertFalse((own / "recovery.token.before").exists())
            self.assertTrue((unrelated / "recovery.token.before").exists())
            self.assertTrue((own / "SHA256SUMS.before-pilot-cleanup").exists())
            self.assertNotIn("PRIVATE_TOKEN_FIXTURE", json.dumps(proof))
            self.assertNotIn("known_12345678", json.dumps(proof))


@unittest.skipUnless(os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0 and OPENSSL,
                     "Native Linux root/OpenSSL required; CI runs this suite with sudo")
class NativeExpiredIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = mtls.NodeMtlsState(self.root / "tls", OPENSSL)
        pending = self.state.prepare_pending_request()
        cert, ca = issue_test_certificate(self.root, self.state.pending_csr, IDENTITY)
        # Historical, ordered validity intervals: CA existed before the leaf.
        (self.root / "index").touch()
        (self.root / "serial").write_text("1000\n")
        (self.root / "issued").mkdir()
        self.ca_config = self.root / "ca.cnf"
        self.ca_config.write_text("\n".join([
            "[ca]", "default_ca=local", "[local]", "default_md=sha256", "default_days=1",
            "database=" + (self.root / "index").as_posix(), "serial=" + (self.root / "serial").as_posix(),
            "new_certs_dir=" + (self.root / "issued").as_posix(), "private_key=" + (self.root / "test-ca.key").as_posix(),
            "certificate=" + ca.as_posix(), "policy=identity", "unique_subject=no", "[identity]", "commonName=supplied",
            "[ca_ext]", "basicConstraints=critical,CA:TRUE", "keyUsage=critical,keyCertSign,cRLSign",
            "[leaf_ext]", "basicConstraints=critical,CA:FALSE", "keyUsage=critical,digitalSignature",
            "extendedKeyUsage=clientAuth", "subjectAltName=URI:" + IDENTITY, ""]))
        self.ca_csr = self.root / "historical-ca.csr"
        run_openssl(["req", "-new", "-key", str(self.root / "test-ca.key"), "-subj", "/CN=WaveMesh Unit Test CA", "-out", str(self.ca_csr)])
        current = datetime.now(timezone.utc)
        date = lambda days: (current + timedelta(days=days)).strftime("%y%m%d%H%M%SZ")
        historical_ca = self.root / "historical-ca.crt"
        run_openssl(["ca", "-batch", "-notext", "-selfsign", "-config", str(self.ca_config), "-in", str(self.ca_csr),
                     "-extensions", "ca_ext", "-startdate", date(-3), "-enddate", date(2), "-out", str(historical_ca)])
        ca.write_bytes(historical_ca.read_bytes())
        run_openssl(["ca", "-batch", "-notext", "-config", str(self.ca_config), "-in", str(self.state.pending_csr),
                     "-extensions", "leaf_ext", "-startdate", date(-2), "-enddate", date(-1), "-out", str(cert)])
        der = self.state._run_bytes(["x509", "-in", str(cert), "-outform", "DER"])
        fingerprint = hashlib.sha256(der).hexdigest()
        generation = self.state.generations_dir / fingerprint[:24]
        generation.mkdir(mode=0o700)
        for name, content in {"client.key": self.state.pending_key.read_bytes(), "client.crt": cert.read_bytes(),
                              "ca.crt": ca.read_bytes(), "client-chain.crt": cert.read_bytes() + ca.read_bytes()}.items():
            mtls.atomic_write_bytes(generation / name, content, 0o600)
        mtls.atomic_write_json(generation / "metadata.json", {"generation": generation.name,
            "identity_uri_hash": mtls.sha256_text(IDENTITY), "public_key_hash": pending.public_key_hash,
            "request_hash": pending.request_hash}, 0o600)
        self.state.clear_pending_request()
        self.state._activate_generation(generation.name)
        _, expiry = expired.certificate_dates(self.state, cert.read_bytes())
        serial = self.state._run_text(["x509", "-in", str(cert), "-noout", "-serial"]).strip().split("=", 1)[1].lower()
        self.baseline = {"acknowledged": True, "revoked": False, "quarantined": False, "compromised": False,
                         "certificateFingerprintHash": fingerprint, "certificateSerialHash": mtls.sha256_text(serial),
                         "publicKeyHash": pending.public_key_hash, "trustedCaBundleSha256": hashlib.sha256(ca.read_bytes()).hexdigest(),
                         "certificateExpiresAt": expiry.isoformat()}
        self.generation = generation

    def tearDown(self):
        self.temp.cleanup()

    def test_expired_inspection_readonly_and_normal_identity_remains_strict(self):
        before = {str(path): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                  for path in self.state.root.rglob("*") if path.is_file() and not path.is_symlink()}
        result = expired.inspect_expired(self.state, IDENTITY, self.baseline)
        self.assertEqual(result["code"], "INTACT_EXPIRED")
        self.assertNotIn("PRIVATE", json.dumps(result))
        with self.assertRaises(mtls.MtlsStateError):
            self.state.active_identity(IDENTITY)
        after = {str(path): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                 for path in self.state.root.rglob("*") if path.is_file() and not path.is_symlink()}
        self.assertEqual(before, after)

    def test_key_san_revocation_fingerprint_and_trust_negatives(self):
        for override in ({"revoked": True}, {"compromised": True}, {"certificateFingerprintHash": "0" * 64},
                         {"certificateSerialHash": "0" * 64}, {"publicKeyHash": "0" * 64}, {"trustedCaBundleSha256": "0" * 64}):
            with self.assertRaises(mtls.MtlsStateError):
                expired.inspect_expired(self.state, IDENTITY, {**self.baseline, **override})
        with self.assertRaises(mtls.MtlsStateError):
            expired.inspect_expired(self.state, IDENTITY.replace("node_12345678", "node_87654321"), self.baseline)

    def test_partial_pending_bad_mode_owner_and_symlink_fail_closed(self):
        self.state.pending_key.touch(mode=0o600)
        with self.assertRaisesRegex(mtls.MtlsStateError, "Partial"):
            expired.inspect_expired(self.state, IDENTITY, self.baseline)
        self.state.pending_key.unlink()
        key = self.generation / "client.key"
        os.chmod(key, 0o640)
        with self.assertRaisesRegex(mtls.MtlsStateError, "permissions"):
            expired.inspect_expired(self.state, IDENTITY, self.baseline)
        os.chmod(key, 0o600)
        os.chown(key, 65534, 65534)
        with self.assertRaisesRegex(mtls.MtlsStateError, "ownership"):
            expired.inspect_expired(self.state, IDENTITY, self.baseline)
        os.chown(key, 0, 0)
        key.rename(self.generation / "retained.key")
        key.symlink_to("retained.key")
        with self.assertRaisesRegex(mtls.MtlsStateError, "unsafe"):
            expired.inspect_expired(self.state, IDENTITY, self.baseline)

    def test_expired_ca_not_suppressed_by_forensic_verify(self):
        expired_ca = self.root / "expired-ca.crt"
        current = datetime.now(timezone.utc)
        date = lambda days: (current + timedelta(days=days)).strftime("%y%m%d%H%M%SZ")
        run_openssl(["ca", "-batch", "-notext", "-selfsign", "-config", str(self.ca_config), "-in", str(self.ca_csr),
                     "-extensions", "ca_ext", "-startdate", date(-3), "-enddate", date(-1), "-out", str(expired_ca)])
        ca = expired_ca.read_bytes()
        mtls.atomic_write_bytes(self.generation / "ca.crt", ca, 0o600)
        mtls.atomic_write_bytes(self.generation / "client-chain.crt", (self.generation / "client.crt").read_bytes() + ca, 0o600)
        with self.assertRaisesRegex(mtls.MtlsStateError, "CA is not currently valid"):
            expired.inspect_expired(self.state, IDENTITY, {**self.baseline, "trustedCaBundleSha256": hashlib.sha256(ca).hexdigest()})

    def test_complete_pending_reused_and_csr_key_mismatch_rejected(self):
        pending = self.state.prepare_pending_request()
        self.assertEqual(expired.inspect_expired(self.state, IDENTITY, self.baseline)["localRequestHash"], pending.request_hash)
        other = mtls.NodeMtlsState(self.root / "other", OPENSSL)
        other.prepare_pending_request()
        mtls.atomic_write_bytes(self.state.pending_csr, other.pending_csr.read_bytes(), 0o600)
        metadata = mtls.read_json_object(self.state.pending_metadata)
        mtls.atomic_write_json(self.state.pending_metadata, {**metadata, "request_hash": self.state._csr_hash(self.state.pending_csr)}, 0o600)
        with self.assertRaisesRegex(mtls.MtlsStateError, "CSR public key"):
            expired.inspect_expired(self.state, IDENTITY, self.baseline)


if __name__ == "__main__":
    unittest.main()
