#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "agent" / "acceptance.py"
SPEC = importlib.util.spec_from_file_location("wave_node_agent_acceptance", MODULE_PATH)
assert SPEC and SPEC.loader
acceptance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = acceptance
SPEC.loader.exec_module(acceptance)


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.etc = root / "etc" / "wavemesh-agent"
        self.install = root / "usr" / "local" / "lib" / "wavemesh-agent"
        self.unit = root / "etc" / "systemd" / "system" / acceptance.SERVICE
        self.rollback = root / "usr" / "local" / "sbin" / "wavemesh-node-agent-rollback"
        self.tls = self.etc / "tls"
        self.paths = acceptance.Paths(
            env_file=self.etc / "agent.env",
            install_dir=self.install,
            unit_file=self.unit,
            rollback_file=self.rollback,
            tls_root=self.tls,
        )
        self.restart_count = "0"
        self.journal = ""
        self.agent_value: dict[str, object] = {
            "ok": True,
            "mode": "observe-only",
            "agent_version": "0.4.0",
            "mtls_mode": "disabled",
            "mtls_state": "BEARER_ONLY",
            "node_id": "node_must_never_be_reported",
            "tenant_id": "tenant_must_never_be_reported",
        }
        self.agent_returncode = 0
        self.openssl_expiry = "notAfter=Jul 27 18:00:00 2030 GMT\n"
        self._prepare_files()

    def _write(self, path: Path, content: str, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)

    def _prepare_files(self) -> None:
        self._write(self.paths.env_file, "WAVEMESH_AGENT_MTLS_MODE=disabled\n", 0o600)
        self._write(self.paths.agent_file, "#!/usr/bin/env python3\n", 0o755)
        for name in ("node_mtls_client.py", "node_mtls_runtime.py", "node_mtls_state.py"):
            self._write(self.install / name, "# installed module\n", 0o644)
        self._write(self.install / "acceptance.py", "#!/usr/bin/env python3\n", 0o755)
        self._write(self.install / "access_runtime.py", "#!/usr/bin/env python3\n", 0o755)
        self._write(self.rollback, "#!/usr/bin/env bash\n", 0o755)
        unit = "\n".join(acceptance.REQUIRED_HARDENING) + "\n"
        self._write(self.unit, unit, 0o644)

    def prepare_shadow(self, outside: bool = False, pending_ack: bool = False) -> None:
        self.agent_value["mtls_mode"] = "shadow"
        self.agent_value["mtls_state"] = "SHADOW_ACTIVE"
        self.tls.mkdir(parents=True, mode=0o700)
        self.tls.chmod(0o700)
        generations = self.tls / "generations"
        generations.mkdir(mode=0o700)
        generations.chmod(0o700)
        generation = (self.root / "outside") if outside else (generations / "generation-1")
        generation.mkdir(mode=0o700)
        generation.chmod(0o700)
        for name in ("client.key", "client.crt", "client-chain.crt", "ca.crt"):
            self._write(generation / name, "private fixture\n", 0o600)
        self._write(self.tls / "runtime.json", "{}\n", 0o600)
        (self.tls / "active").symlink_to(generation, target_is_directory=True)
        if pending_ack:
            self._write(self.tls / "acknowledgement.pending.json", "{}\n", 0o600)

    def runner(self, argv: tuple[str, ...]) -> acceptance.CommandResult:
        if argv[0] == "systemctl":
            output = (
                "ActiveState=active\n"
                "UnitFileState=enabled\n"
                f"NRestarts={self.restart_count}\n"
                "MainPID=123\n"
            )
            return acceptance.CommandResult(0, output.encode())
        if argv[0] == "/usr/bin/python3":
            return acceptance.CommandResult(
                self.agent_returncode,
                json.dumps(self.agent_value).encode() if self.agent_returncode == 0 else b"not-json",
            )
        if argv[0] == "journalctl":
            return acceptance.CommandResult(0, self.journal.encode())
        if argv[0] == "openssl":
            return acceptance.CommandResult(0, self.openssl_expiry.encode())
        raise AssertionError(f"unexpected command kind: {argv[0]}")

    def collect(self, phase: str) -> dict[str, object]:
        return acceptance.AcceptanceCollector(
            phase,
            paths=self.paths,
            runner=self.runner,
            expected_uid=os.getuid() if hasattr(os, "getuid") else 0,
            now=datetime(2026, 7, 27, tzinfo=timezone.utc),
        ).collect()


class AcceptanceTests(unittest.TestCase):
    def test_baseline_passes_and_identifiers_are_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            report = fixture.collect("baseline")
            serialized = json.dumps(report)
            self.assertEqual(report["result"], "PASS")
            self.assertNotIn("node_must_never_be_reported", serialized)
            self.assertNotIn("tenant_must_never_be_reported", serialized)

    def test_disabled_requires_installed_shadow_package_but_stays_bearer_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Fixture(Path(directory)).collect("disabled")
            self.assertEqual(report["result"], "PASS")
            self.assertEqual(report["agent"]["mtls_state"], "BEARER_ONLY")

    def test_baseline_blocks_an_already_enabled_shadow_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.agent_value["mtls_mode"] = "shadow"
            fixture.agent_value["mtls_state"] = "SHADOW_READY"
            report = fixture.collect("baseline")
            self.assertIn("AGENT_BASELINE_NOT_BEARER_ONLY", report["blocking_codes"])

    def test_restart_count_blocks_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.restart_count = "2"
            report = fixture.collect("baseline")
            self.assertIn("SERVICE_RESTART_COUNT_NONZERO", report["blocking_codes"])

    @unittest.skipIf(os.name == "nt", "Windows does not preserve POSIX modes")
    def test_unsafe_environment_mode_blocks_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.paths.env_file.chmod(0o644)
            report = fixture.collect("baseline")
            self.assertIn("UNSAFE_AGENT_ENV", report["blocking_codes"])

    def test_malformed_agent_output_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.agent_returncode = 1
            report = fixture.collect("baseline")
            self.assertIn("AGENT_CHECK_FAILED", report["blocking_codes"])
            self.assertNotIn("not-json", json.dumps(report))

    def test_rollback_accepts_pre_mtls_agent_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.agent_value.pop("mtls_mode")
            fixture.agent_value.pop("mtls_state")
            report = fixture.collect("rollback")
            self.assertEqual(report["result"], "PASS")

    def test_log_values_are_replaced_with_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            leaked_token = "wvn_" + ("a" * 40)
            leaked_identity = "spiffe://wavevpn/staging/tenant/example/node/example"
            fixture.journal = f"token={leaked_token} identity={leaked_identity}\n"
            report = fixture.collect("baseline")
            serialized = json.dumps(report)
            self.assertEqual(report["log_findings"]["bearer_token"], 1)
            self.assertEqual(report["log_findings"]["identity_uri"], 1)
            self.assertIn("LOG_SECRET_PATTERN_FOUND", report["blocking_codes"])
            self.assertNotIn(leaked_token, serialized)
            self.assertNotIn(leaked_identity, serialized)

    def test_command_output_is_bounded(self) -> None:
        result = acceptance.run_bounded(
            (
                sys.executable,
                "-c",
                f"import sys; sys.stdout.write('x' * {acceptance.MAX_OUTPUT_BYTES + 1})",
            )
        )
        self.assertEqual(result.returncode, 126)
        self.assertEqual(result.stdout, b"")

    @unittest.skipIf(os.name == "nt", "Windows symlink creation is not reliably available")
    def test_shadow_passes_with_contained_private_active_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.prepare_shadow()
            report = fixture.collect("shadow")
            self.assertEqual(report["result"], "PASS")
            self.assertTrue(report["certificate"]["valid"])

    @unittest.skipIf(os.name == "nt", "Windows symlink creation is not reliably available")
    def test_shadow_blocks_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.prepare_shadow(outside=True)
            report = fixture.collect("shadow")
            self.assertIn("TLS_ACTIVE_GENERATION_UNSAFE", report["blocking_codes"])

    @unittest.skipIf(os.name == "nt", "Windows symlink creation is not reliably available")
    def test_shadow_blocks_expired_certificate_and_pending_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.prepare_shadow(pending_ack=True)
            fixture.openssl_expiry = "notAfter=Jul 27 18:00:00 2020 GMT\n"
            report = fixture.collect("shadow")
            self.assertIn("TLS_ACKNOWLEDGEMENT_PENDING", report["blocking_codes"])
            self.assertIn("TLS_CERTIFICATE_INVALID_OR_EXPIRED", report["blocking_codes"])


if __name__ == "__main__":
    unittest.main()
