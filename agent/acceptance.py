#!/usr/bin/env python3
"""Read-only, sanitized acceptance evidence for the WaveMesh Node Agent."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Callable, Sequence

SERVICE = "wavemesh-node-agent.service"
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
PHASES = ("baseline", "disabled", "shadow", "rollback")
REQUIRED_HARDENING = (
    "User=root",
    "Group=root",
    "UMask=0077",
    "NoNewPrivileges=true",
    "ProtectSystem=strict",
    "ProtectHome=true",
    "ReadWritePaths=/etc/wavemesh-agent /etc/wavemesh-node /var/lib/wavemesh-agent",
)
LEAK_PATTERNS = {
    "bearer_token": re.compile(r"\bwvn_[A-Za-z0-9_-]{20,}\b"),
    "digest": re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])"),
    "identity_uri": re.compile(r"\bspiffe://", re.IGNORECASE),
    "pem_envelope": re.compile(r"-{5}BEGIN [A-Z0-9 ]+-{5}"),
    "vpn_uri": re.compile(r"\b(?:vless|vmess|trojan|hysteria2?)://", re.IGNORECASE),
}


@dataclass(frozen=True)
class Paths:
    env_file: Path = Path("/etc/wavemesh-agent/agent.env")
    install_dir: Path = Path("/usr/local/lib/wavemesh-agent")
    unit_file: Path = Path("/etc/systemd/system/wavemesh-node-agent.service")
    rollback_file: Path = Path("/usr/local/sbin/wavemesh-node-agent-rollback")
    tls_root: Path = Path("/etc/wavemesh-agent/tls")

    @property
    def agent_file(self) -> Path:
        return self.install_dir / "node_agent.py"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes


Runner = Callable[[Sequence[str]], CommandResult]


def run_bounded(argv: Sequence[str]) -> CommandResult:
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                list(argv),
                stdout=output,
                stderr=subprocess.DEVNULL,
            )
            try:
                returncode = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                return CommandResult(124, b"")
            output.seek(0)
            value = output.read(MAX_OUTPUT_BYTES + 1)
    except (OSError, subprocess.SubprocessError):
        return CommandResult(127, b"")
    if len(value) > MAX_OUTPUT_BYTES:
        return CommandResult(126, b"")
    return CommandResult(returncode, value)


class AcceptanceCollector:
    def __init__(
        self,
        phase: str,
        paths: Paths = Paths(),
        runner: Runner = run_bounded,
        expected_uid: int = 0,
        since: str = "-30 minutes",
        now: datetime | None = None,
    ) -> None:
        if phase not in PHASES:
            raise ValueError("unsupported phase")
        self.phase = phase
        self.paths = paths
        self.runner = runner
        self.expected_uid = expected_uid
        self.since = since
        self.now = now or datetime.now(timezone.utc)
        self.blockers: set[str] = set()
        self.filesystem: dict[str, bool] = {}

    def collect(self) -> dict[str, object]:
        service = self._service()
        agent = self._agent()
        self._filesystem(agent)
        certificate = self._certificate(agent)
        findings = self._journal()
        if any(findings.values()):
            self.blockers.add("LOG_SECRET_PATTERN_FOUND")
        return {
            "schema_version": 1,
            "phase": self.phase,
            "result": "PASS" if not self.blockers else "BLOCKED",
            "blocking_codes": sorted(self.blockers),
            "service": service,
            "agent": agent,
            "filesystem": dict(sorted(self.filesystem.items())),
            "certificate": certificate,
            "log_findings": findings,
        }

    def _service(self) -> dict[str, object]:
        result = self.runner(
            (
                "systemctl",
                "show",
                SERVICE,
                "--no-pager",
                "--property=ActiveState",
                "--property=UnitFileState",
                "--property=NRestarts",
                "--property=MainPID",
            )
        )
        values: dict[str, str] = {}
        if result.returncode == 0:
            for line in result.stdout.decode("utf-8", "replace").splitlines():
                key, separator, value = line.partition("=")
                if separator and key in {"ActiveState", "UnitFileState", "NRestarts", "MainPID"}:
                    values[key] = value
        active = values.get("ActiveState") == "active"
        enabled = values.get("UnitFileState") == "enabled"
        restart_count_zero = values.get("NRestarts") == "0"
        pid_running = values.get("MainPID", "").isdigit() and int(values["MainPID"]) > 0
        if not active:
            self.blockers.add("SERVICE_NOT_ACTIVE")
        if not enabled:
            self.blockers.add("SERVICE_NOT_ENABLED")
        if not restart_count_zero:
            self.blockers.add("SERVICE_RESTART_COUNT_NONZERO")
        if not pid_running:
            self.blockers.add("SERVICE_PID_INVALID")
        return {
            "active": active,
            "enabled": enabled,
            "restart_count_zero": restart_count_zero,
            "pid_running": pid_running,
        }

    def _agent(self) -> dict[str, object]:
        result = self.runner(
            (
                "/usr/bin/python3",
                str(self.paths.agent_file),
                "check",
                "--env-file",
                str(self.paths.env_file),
            )
        )
        value: dict[str, object] = {}
        if result.returncode == 0:
            try:
                decoded = json.loads(result.stdout.decode("utf-8"))
                if isinstance(decoded, dict):
                    value = decoded
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        check_ok = value.get("ok") is True
        version = value.get("agent_version")
        safe_version = version if isinstance(version, str) and SAFE_VALUE.fullmatch(version) else None
        mode = value.get("mode") if value.get("mode") == "observe-only" else None
        mtls_mode = value.get("mtls_mode")
        mtls_state = value.get("mtls_state")
        if mtls_mode not in {"disabled", "shadow"}:
            mtls_mode = None
        if mtls_state not in {
            "BEARER_ONLY",
            "ENROLLING",
            "SHADOW_READY",
            "SHADOW_ACTIVE",
            "ROTATING",
            "FALLBACK",
            "BLOCKED",
        }:
            mtls_state = None
        if not check_ok:
            self.blockers.add("AGENT_CHECK_FAILED")
        if mode != "observe-only":
            self.blockers.add("AGENT_MODE_INVALID")
        if self.phase == "baseline":
            if mtls_mode not in {None, "disabled"} or mtls_state not in {None, "BEARER_ONLY"}:
                self.blockers.add("AGENT_BASELINE_NOT_BEARER_ONLY")
        if self.phase == "disabled" and (mtls_mode != "disabled" or mtls_state != "BEARER_ONLY"):
            self.blockers.add("AGENT_NOT_BEARER_ONLY")
        if self.phase == "shadow" and (mtls_mode != "shadow" or mtls_state != "SHADOW_ACTIVE"):
            self.blockers.add("AGENT_SHADOW_NOT_ACTIVE")
        if self.phase == "rollback":
            # Older rollback targets may predate the mTLS fields.
            if mtls_mode not in {None, "disabled"} or mtls_state not in {None, "BEARER_ONLY"}:
                self.blockers.add("AGENT_ROLLBACK_NOT_BEARER_ONLY")
        return {
            "check_ok": check_ok,
            "version": safe_version,
            "mode": mode,
            "mtls_mode": mtls_mode,
            "mtls_state": mtls_state,
        }

    def _filesystem(self, agent: dict[str, object]) -> None:
        required = {
            "agent_env": (self.paths.env_file, 0o600),
            "agent_main": (self.paths.agent_file, 0o755),
            "service_unit": (self.paths.unit_file, 0o644),
        }
        if self.phase in {"disabled", "shadow"}:
            required.update(
                {
                    "mtls_client": (self.paths.install_dir / "node_mtls_client.py", 0o644),
                    "mtls_runtime": (self.paths.install_dir / "node_mtls_runtime.py", 0o644),
                    "mtls_state": (self.paths.install_dir / "node_mtls_state.py", 0o644),
                    "acceptance": (self.paths.install_dir / "acceptance.py", 0o755),
                    "access_runtime": (self.paths.install_dir / "access_runtime.py", 0o755),
                    "rollback": (self.paths.rollback_file, 0o755),
                }
            )
        for label, (path, mode) in required.items():
            valid = self._safe_file(path, mode)
            self.filesystem[label] = valid
            if not valid:
                self.blockers.add(f"UNSAFE_{label.upper()}")
        unit_hardened = self._unit_hardened()
        self.filesystem["unit_hardening"] = unit_hardened
        if not unit_hardened:
            self.blockers.add("UNIT_HARDENING_INVALID")
        env_pem_free = self._env_pem_free()
        self.filesystem["env_pem_free"] = env_pem_free
        if not env_pem_free:
            self.blockers.add("ENV_PEM_MATERIAL_FOUND")
        if agent.get("mtls_mode") == "shadow" or self.phase == "shadow":
            self._tls_filesystem()

    def _safe_file(self, path: Path, mode: int) -> bool:
        try:
            value = path.lstat()
        except OSError:
            return False
        return (
            stat.S_ISREG(value.st_mode)
            and not path.is_symlink()
            and (os.name == "nt" or stat.S_IMODE(value.st_mode) == mode)
            and (os.name == "nt" or value.st_uid == self.expected_uid)
        )

    def _safe_directory(self, path: Path, mode: int) -> bool:
        try:
            value = path.lstat()
        except OSError:
            return False
        return (
            stat.S_ISDIR(value.st_mode)
            and not path.is_symlink()
            and (os.name == "nt" or stat.S_IMODE(value.st_mode) == mode)
            and (os.name == "nt" or value.st_uid == self.expected_uid)
        )

    def _unit_hardened(self) -> bool:
        try:
            if self.paths.unit_file.is_symlink():
                return False
            lines = set(self.paths.unit_file.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError):
            return False
        return all(item in lines for item in REQUIRED_HARDENING)

    def _env_pem_free(self) -> bool:
        try:
            if self.paths.env_file.is_symlink():
                return False
            value = self.paths.env_file.read_bytes()
        except OSError:
            return False
        return not LEAK_PATTERNS["pem_envelope"].search(value.decode("ascii", "ignore"))

    def _tls_filesystem(self) -> None:
        root_ok = self._safe_directory(self.paths.tls_root, 0o700)
        generations = self.paths.tls_root / "generations"
        generations_ok = self._safe_directory(generations, 0o700)
        self.filesystem["tls_root"] = root_ok
        self.filesystem["tls_generations"] = generations_ok
        if not root_ok or not generations_ok:
            self.blockers.add("TLS_LAYOUT_UNSAFE")
            return
        active = self.paths.tls_root / "active"
        try:
            active_ok = active.is_symlink()
            resolved = active.resolve(strict=True)
            contained = resolved.parent == generations.resolve(strict=True)
        except OSError:
            active_ok = False
            contained = False
            resolved = active
        self.filesystem["tls_active_symlink"] = active_ok and contained
        if not active_ok or not contained or not self._safe_directory(resolved, 0o700):
            self.blockers.add("TLS_ACTIVE_GENERATION_UNSAFE")
            return
        for name in ("client.key", "client.crt", "client-chain.crt", "ca.crt"):
            label = "tls_" + name.replace(".", "_").replace("-", "_")
            valid = self._safe_file(resolved / name, 0o600)
            self.filesystem[label] = valid
            if not valid:
                self.blockers.add("TLS_ACTIVE_GENERATION_UNSAFE")
        runtime_ok = self._safe_file(self.paths.tls_root / "runtime.json", 0o600)
        self.filesystem["tls_runtime"] = runtime_ok
        if not runtime_ok:
            self.blockers.add("TLS_RUNTIME_UNSAFE")
        acknowledgement_clear = not (self.paths.tls_root / "acknowledgement.pending.json").exists()
        self.filesystem["tls_acknowledgement_clear"] = acknowledgement_clear
        if not acknowledgement_clear:
            self.blockers.add("TLS_ACKNOWLEDGEMENT_PENDING")

    def _certificate(self, agent: dict[str, object]) -> dict[str, object]:
        if agent.get("mtls_mode") != "shadow" and self.phase != "shadow":
            return {"present": False, "valid": None, "expires_at": None}
        active = self.paths.tls_root / "active"
        certificate = active / "client.crt"
        result = self.runner(("openssl", "x509", "-enddate", "-noout", "-in", str(certificate)))
        expires_at: str | None = None
        valid = False
        if result.returncode == 0 and len(result.stdout) <= 256:
            text = result.stdout.decode("ascii", "ignore").strip()
            prefix = "notAfter="
            if text.startswith(prefix):
                try:
                    expiry = parsedate_to_datetime(text[len(prefix) :]).astimezone(timezone.utc)
                    valid = expiry > self.now
                    expires_at = expiry.isoformat().replace("+00:00", "Z")
                except (TypeError, ValueError, OverflowError):
                    pass
        if not valid:
            self.blockers.add("TLS_CERTIFICATE_INVALID_OR_EXPIRED")
        return {"present": certificate.exists(), "valid": valid, "expires_at": expires_at}

    def _journal(self) -> dict[str, int]:
        result = self.runner(
            (
                "journalctl",
                "-u",
                SERVICE,
                "--since",
                self.since,
                "--lines=2000",
                "--output=cat",
                "--no-pager",
            )
        )
        if result.returncode != 0:
            self.blockers.add("JOURNAL_READ_FAILED")
            text = ""
        else:
            text = result.stdout.decode("utf-8", "replace")
        return {name: len(pattern.findall(text)) for name, pattern in sorted(LEAK_PATTERNS.items())}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect sanitized Node Agent acceptance evidence")
    parser.add_argument("phase", choices=PHASES)
    parser.add_argument(
        "--since",
        default="-30 minutes",
        help="journalctl --since value; the default covers the last 30 minutes",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = AcceptanceCollector(args.phase, since=args.since).collect()
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())