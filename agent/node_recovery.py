#!/usr/bin/env python3
"""One-time, node-scoped recovery for an expired WaveMesh Agent auth chain."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from urllib import error, request

try:
    from node_agent import (
        AGENT_VERSION,
        AgentError,
        format_timestamp,
        generate_token,
        parse_timestamp,
        read_env_file,
        token_hash,
        valid_token,
        write_env_file,
    )
    from node_mtls_state import (
        NodeMtlsState,
        PRIVATE_FILE_MODE,
        atomic_write_json,
    )
except ImportError:  # pragma: no cover - package-style imports in tests
    from .node_agent import (
        AGENT_VERSION,
        AgentError,
        format_timestamp,
        generate_token,
        parse_timestamp,
        read_env_file,
        token_hash,
        valid_token,
        write_env_file,
    )
    from .node_mtls_state import (
        NodeMtlsState,
        PRIVATE_FILE_MODE,
        atomic_write_json,
    )

DEFAULT_ENV_FILE = Path("/etc/wavemesh-agent/agent.env")
DEFAULT_RECOVERY_TOKEN_FILE = Path("/etc/wavemesh-agent/recovery.token")
RECOVERY_TOKEN_PATTERN = re.compile(r"^wvr_[A-Za-z0-9_-]{32,128}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class RecoveryError(AgentError):
    pass


class RecoveryClient:
    def __init__(
        self,
        env_file: Path,
        recovery_token_file: Path,
        external_node_id: str,
        timeout_seconds: int = 30,
    ) -> None:
        self.env_file = env_file
        self.recovery_token_file = recovery_token_file
        self.external_node_id = validate_external_node_id(external_node_id)
        self.timeout_seconds = timeout_seconds
        self.values = read_env_file(env_file)
        self.api_base = require_https_api_base(self.values.get("WAVEMESH_API_BASE"))
        self.node_id = require_identifier(self.values.get("WAVEMESH_NODE_ID"), "Node ID")
        self.tenant_id = require_identifier(self.values.get("WAVEMESH_TENANT_ID"), "Tenant ID")
        self.mtls_mode = self.values.get("WAVEMESH_AGENT_MTLS_MODE", "disabled")
        if self.mtls_mode != "shadow":
            raise RecoveryError("Node recovery requires WAVEMESH_AGENT_MTLS_MODE=shadow")
        self.state_root = Path(
            self.values.get("WAVEMESH_AGENT_MTLS_STATE_ROOT", "/etc/wavemesh-agent/tls")
        )
        self.state = NodeMtlsState(self.state_root)
        self.pending_token_file = env_file.with_name("recovery.pending")
        self.accepted_file = env_file.with_name("recovery.accepted.json")
        self.rotation_pending_file = env_file.with_name("rotation.pending")

    def check(self) -> dict[str, Any]:
        recovery_token = read_restricted_token(
            self.recovery_token_file,
            RECOVERY_TOKEN_PATTERN,
            "Recovery token",
        )
        del recovery_token
        pending = self._load_pending_token(required=False)
        accepted = self._load_accepted(required=False)
        runtime_state = self._runtime_state()
        return {
            "ok": True,
            "external_node_id": self.external_node_id,
            "node_id": self.node_id,
            "tenant_id": self.tenant_id,
            "mtls_mode": self.mtls_mode,
            "mtls_runtime_state": runtime_state,
            "pending_recovery": pending is not None,
            "accepted_recovery": accepted is not None,
            "agent_version": AGENT_VERSION,
        }

    def apply(self) -> dict[str, Any]:
        accepted = self._load_accepted(required=False)
        if accepted is None:
            recovery_token = read_restricted_token(
                self.recovery_token_file,
                RECOVERY_TOKEN_PATTERN,
                "Recovery token",
            )
            pending_token = self._load_or_create_pending_token()
            pending_certificate = self.state.prepare_pending_request()
            response = self._request_recovery(
                recovery_token,
                token_hash(pending_token),
                pending_certificate.public_key_hash,
            )
            accepted = self._validate_response(response, token_hash(pending_token))
            atomic_write_json(self.accepted_file, accepted, PRIVATE_FILE_MODE)
        result = self._finalize(accepted)
        return result

    def _request_recovery(
        self,
        recovery_token: str,
        next_token_hash: str,
        public_key_hash: str,
    ) -> dict[str, Any]:
        payload = {
            "external_node_id": self.external_node_id,
            "agent_version": AGENT_VERSION,
            "next_token_hash": next_token_hash,
            "public_key_hash": public_key_hash,
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            f"{self.api_base}/internal/v1/nodes/recover",
            data=body,
            headers={
                "Authorization": f"Bearer {recovery_token}",
                "Content-Type": "application/json",
                "User-Agent": f"wavemesh-node-recovery/{AGENT_VERSION}",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                status = response.status
                raw = response.read()
        except error.HTTPError as exc:
            problem = parse_problem(exc.read())
            code = safe_code(problem.get("code"))
            retryable = bool(problem.get("retryable", False))
            raise RecoveryError(
                f"Recovery API rejected the request: status={exc.code} code={code} retryable={str(retryable).lower()}"
            ) from None
        except error.URLError as exc:
            raise RecoveryError("Recovery API is temporarily unreachable") from exc
        if status != 201:
            raise RecoveryError(f"Recovery API returned unexpected status {status}")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError("Recovery API returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise RecoveryError("Recovery API returned a non-object response")
        return decoded

    def _validate_response(
        self,
        response: dict[str, Any],
        pending_token_hash: str,
    ) -> dict[str, Any]:
        node_id = require_identifier(response.get("node_id"), "Recovered Node ID")
        tenant_id = require_identifier(response.get("tenant_id"), "Recovered tenant ID")
        if node_id != self.node_id or tenant_id != self.tenant_id:
            raise RecoveryError("Recovery response identity does not match the local Agent")
        if response.get("auth_mode") != "temporary_bearer":
            raise RecoveryError("Recovery response auth mode is invalid")
        if response.get("recovery_state") != "ENROLLING":
            raise RecoveryError("Recovery response state is invalid")
        expires_at = parse_timestamp(require_string(response, "expires_at"))
        now = datetime.now(timezone.utc)
        if expires_at <= now or expires_at > now + timedelta(hours=25):
            raise RecoveryError("Recovered bearer expiry is outside the accepted window")
        already_processed = response.get("already_processed")
        if not isinstance(already_processed, bool):
            raise RecoveryError("Recovery response replay state is invalid")
        return {
            "accepted_at": format_timestamp(now),
            "already_processed": already_processed,
            "expires_at": format_timestamp(expires_at),
            "next_token_hash": pending_token_hash,
            "node_id": node_id,
            "tenant_id": tenant_id,
            "version": 1,
        }

    def _finalize(self, accepted: dict[str, Any]) -> dict[str, Any]:
        expected_hash = require_sha256(accepted, "next_token_hash")
        pending_token = self._load_pending_token(required=False)
        current_token = self.values.get("WAVEMESH_AGENT_TOKEN", "")
        if pending_token is not None:
            local_token = pending_token
        elif valid_token(current_token) and token_hash(current_token) == expected_hash:
            local_token = current_token
        else:
            raise RecoveryError("Accepted recovery has no matching local pending credential")
        if token_hash(local_token) != expected_hash:
            raise RecoveryError("Pending recovery credential hash does not match acceptance")

        expires_at = parse_timestamp(require_string(accepted, "expires_at"))
        values = read_env_file(self.env_file)
        values["WAVEMESH_AGENT_TOKEN"] = local_token
        values["WAVEMESH_AGENT_TOKEN_EXPIRES_AT"] = format_timestamp(expires_at)
        write_env_file(self.env_file, values)
        self.values = values

        safe_unlink(self.rotation_pending_file)
        pending_acknowledgement = self.state.pending_acknowledgement()
        if pending_acknowledgement is not None:
            self.state.clear_pending_acknowledgement(pending_acknowledgement.credential_id)
        deactivated_generation = self.state.deactivate_active_identity()
        atomic_write_json(
            self.state_root / "runtime.json",
            {
                "code": None,
                "retry_at": None,
                "retry_attempts": 0,
                "state": "BEARER_ONLY",
                "version": 1,
            },
            PRIVATE_FILE_MODE,
        )

        safe_unlink(self.pending_token_file)
        safe_unlink(self.recovery_token_file)
        safe_unlink(self.accepted_file)
        return {
            "ok": True,
            "node_id": self.node_id,
            "tenant_id": self.tenant_id,
            "token_expires_at": format_timestamp(expires_at),
            "mtls_runtime_state": "BEARER_ONLY",
            "active_generation_deactivated": deactivated_generation is not None,
            "agent_version": AGENT_VERSION,
        }

    def _load_or_create_pending_token(self) -> str:
        existing = self._load_pending_token(required=False)
        if existing is not None:
            return existing
        pending = generate_token()
        write_env_file(
            self.pending_token_file,
            {"WAVEMESH_PENDING_RECOVERY_AGENT_TOKEN": pending},
        )
        return pending

    def _load_pending_token(self, required: bool) -> str | None:
        if not self.pending_token_file.exists():
            if required:
                raise RecoveryError("Pending recovery credential is missing")
            return None
        ensure_restricted_regular_file(self.pending_token_file, "Pending recovery credential")
        values = read_env_file(self.pending_token_file)
        pending = values.get("WAVEMESH_PENDING_RECOVERY_AGENT_TOKEN", "")
        if not valid_token(pending):
            raise RecoveryError("Pending recovery credential has an invalid format")
        return pending

    def _load_accepted(self, required: bool) -> dict[str, Any] | None:
        if not self.accepted_file.exists():
            if required:
                raise RecoveryError("Recovery acceptance marker is missing")
            return None
        ensure_restricted_regular_file(self.accepted_file, "Recovery acceptance marker")
        try:
            value = json.loads(self.accepted_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError("Recovery acceptance marker is unreadable") from exc
        if not isinstance(value, dict) or value.get("version") != 1:
            raise RecoveryError("Recovery acceptance marker is invalid")
        if require_identifier(value.get("node_id"), "Accepted Node ID") != self.node_id:
            raise RecoveryError("Recovery acceptance marker Node ID is invalid")
        if require_identifier(value.get("tenant_id"), "Accepted tenant ID") != self.tenant_id:
            raise RecoveryError("Recovery acceptance marker tenant ID is invalid")
        require_sha256(value, "next_token_hash")
        parse_timestamp(require_string(value, "expires_at"))
        return value

    def _runtime_state(self) -> str:
        runtime_path = self.state_root / "runtime.json"
        if not runtime_path.is_file():
            return "MISSING"
        try:
            value = json.loads(runtime_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return "INVALID"
        state = value.get("state") if isinstance(value, dict) else None
        return str(state) if isinstance(state, str) else "INVALID"


def ensure_restricted_regular_file(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise RecoveryError(f"{label} file is missing") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RecoveryError(f"{label} path is unsafe")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE:
        raise RecoveryError(f"{label} permissions must be 0600")


def read_restricted_token(path: Path, pattern: re.Pattern[str], label: str) -> str:
    ensure_restricted_regular_file(path, label)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise RecoveryError(f"{label} is unreadable") from exc
    if not pattern.fullmatch(value):
        raise RecoveryError(f"{label} has an invalid format")
    return value


def require_https_api_base(value: str | None) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized.startswith("https://") or len(normalized) > 512:
        raise RecoveryError("WAVEMESH_API_BASE must use HTTPS")
    return normalized


def validate_external_node_id(value: str) -> str:
    normalized = value.strip()
    if not 3 <= len(normalized) <= 128 or not all(
        character.isalnum() or character in "._:-" for character in normalized
    ):
        raise RecoveryError("External Node ID is invalid")
    return normalized


def require_identifier(value: Any, label: str) -> str:
    item = str(value or "")
    if not 8 <= len(item) <= 128 or not all(
        character.isalnum() or character in "_-" for character in item
    ):
        raise RecoveryError(f"{label} is invalid")
    return item


def require_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RecoveryError(f"Recovery response is missing {key}")
    return item


def require_sha256(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not SHA256_PATTERN.fullmatch(item):
        raise RecoveryError(f"Recovery marker field is invalid: {key}")
    return item


def parse_problem(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("error"), dict):
        return value["error"]
    return value


def safe_code(value: Any) -> str:
    normalized = "".join(
        character if character.isalnum() else "_"
        for character in str(value or "HTTP_ERROR").upper()
    )[:96]
    return normalized if normalized and normalized[0].isalpha() else "HTTP_ERROR"


def safe_unlink(path: Path) -> None:
    try:
        if path.is_symlink():
            raise RecoveryError(f"Refusing to remove unsafe recovery path: {path.name}")
        path.unlink()
    except FileNotFoundError:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WaveMesh node credential recovery")
    parser.add_argument("command", choices=("check", "apply"))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_RECOVERY_TOKEN_FILE)
    parser.add_argument("--external-node-id", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=30, choices=range(5, 121), metavar="5..120")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = RecoveryClient(
        args.env_file,
        args.token_file,
        args.external_node_id,
        args.timeout_seconds,
    )
    result = client.check() if args.command == "check" else client.apply()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
