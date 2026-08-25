#!/usr/bin/env python3
"""Direct CSR, node-scoped mTLS recovery for the WaveMesh Node Agent."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
        parse_timestamp,
        read_env_file,
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
        parse_timestamp,
        read_env_file,
    )
    from .node_mtls_state import (
        NodeMtlsState,
        PRIVATE_FILE_MODE,
        atomic_write_json,
    )

DEFAULT_ENV_FILE = Path("/etc/wavemesh-agent/agent.env")
DEFAULT_RECOVERY_TOKEN_FILE = Path("/etc/wavemesh-agent/recovery.token")
RECOVERY_TOKEN_PATTERN = re.compile(r"^wvr_[A-Za-z0-9_-]{32,128}$")
SAFE_SCOPE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
SAFE_CREDENTIAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
SAFE_ENVIRONMENT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
MAX_RESPONSE_BYTES = 256 * 1024
MAX_PEM_BYTES = 128 * 1024
MARKER_VERSION = 2


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
        self.node_id = require_scope_id(self.values.get("WAVEMESH_NODE_ID"), "Node ID")
        self.tenant_id = require_scope_id(self.values.get("WAVEMESH_TENANT_ID"), "Tenant ID")
        self.mtls_mode = self.values.get("WAVEMESH_AGENT_MTLS_MODE", "disabled")
        if self.mtls_mode != "shadow":
            raise RecoveryError("Node recovery requires WAVEMESH_AGENT_MTLS_MODE=shadow")
        self.environment = require_environment(
            self.values.get("WAVEMESH_AGENT_MTLS_ENVIRONMENT", "staging")
        )
        self.state_root = Path(
            self.values.get("WAVEMESH_AGENT_MTLS_STATE_ROOT", "/etc/wavemesh-agent/tls")
        )
        self.state = NodeMtlsState(self.state_root)
        self.pending_file = env_file.with_name("recovery.pending")
        self.legacy_accepted_file = env_file.with_name("recovery.accepted.json")
        self.runtime_file = self.state_root / "runtime.json"

    @property
    def expected_identity_uri(self) -> str:
        return (
            f"spiffe://wavevpn/{self.environment}/tenant/"
            f"{self.tenant_id}/node/{self.node_id}"
        )

    def check(self) -> dict[str, Any]:
        self._assert_no_legacy_state()
        marker = self._load_marker(required=False)
        pending_present = self._pending_request_present()
        acknowledgement = self.state.pending_acknowledgement()

        if marker is not None:
            self._validate_local_state(marker, pending_present, acknowledgement)
            if marker.get("acknowledged_at") is None:
                recovery_token = read_restricted_token(
                    self.recovery_token_file,
                    RECOVERY_TOKEN_PATTERN,
                    "Recovery token",
                )
                del recovery_token
        else:
            recovery_token = read_restricted_token(
                self.recovery_token_file,
                RECOVERY_TOKEN_PATTERN,
                "Recovery token",
            )
            del recovery_token
            if acknowledgement is not None:
                raise RecoveryError(
                    "An unrelated mTLS acknowledgement is pending; refusing recovery"
                )

        return {
            "ok": True,
            "external_node_id": self.external_node_id,
            "node_id": self.node_id,
            "tenant_id": self.tenant_id,
            "mtls_mode": self.mtls_mode,
            "mtls_runtime_state": self._runtime_state(),
            "pending_recovery": marker is not None,
            "pending_certificate_request": pending_present,
            "pending_acknowledgement": acknowledgement is not None,
            "cleanup_pending": bool(marker and marker.get("acknowledged_at")),
            "agent_version": AGENT_VERSION,
        }

    def apply(self) -> dict[str, Any]:
        self._assert_no_legacy_state()
        marker = self._load_marker(required=False)
        acknowledgement = self.state.pending_acknowledgement()

        if marker is not None and marker.get("acknowledged_at") is not None:
            self._validate_local_state(
                marker,
                self._pending_request_present(),
                acknowledgement,
            )
            return self._finish_acknowledged_cleanup(marker, acknowledgement)

        recovery_token = read_restricted_token(
            self.recovery_token_file,
            RECOVERY_TOKEN_PATTERN,
            "Recovery token",
        )

        if marker is None:
            if acknowledgement is not None:
                raise RecoveryError(
                    "An unrelated mTLS acknowledgement is pending; refusing recovery"
                )
            pending = self.state.prepare_pending_request()
            marker = self._new_marker(pending)
            self._write_marker(marker)
        else:
            pending_present = self._pending_request_present()
            self._validate_local_state(marker, pending_present, acknowledgement)
            pending = (
                self.state.prepare_pending_request()
                if pending_present
                else None
            )
            if pending is not None:
                self._assert_pending_matches_marker(marker, pending)

        credential_id = optional_credential_id(marker.get("credential_id"))
        acknowledgement = self.state.pending_acknowledgement()

        if credential_id is None:
            if pending is None:
                raise RecoveryError(
                    "Pending recovery request is missing before server acceptance"
                )
            response = self._api_json(
                recovery_token,
                "POST",
                "internal/v1/nodes/recover/certificates",
                {
                    "tenant_id": self.tenant_id,
                    "node_id": self.node_id,
                    "csr": pending.csr_pem,
                },
                expected=(201,),
            )
            delivery = self._validate_delivery(response)
            marker = self._bind_credential(marker, delivery["credential_id"])
            self._write_marker(marker)
            credential_id = delivery["credential_id"]
            acknowledgement = self.state.record_pending_acknowledgement(
                credential_id,
                require_sha256(marker, "request_hash"),
                delivery["delivery_expires_at"],
            )
            active = self.state.activate_pending_certificate(
                delivery["certificate"],
                delivery["chain"],
                self.expected_identity_uri,
            )
            self._assert_active_matches_marker(active, marker)
            generation = active.generation
        elif self._pending_request_present():
            if pending is None:
                pending = self.state.prepare_pending_request()
                self._assert_pending_matches_marker(marker, pending)
            response = self._api_json(
                recovery_token,
                "GET",
                f"internal/v1/nodes/recover/certificates/{credential_id}",
                None,
                expected=(200,),
            )
            delivery = self._validate_delivery(response, expected_credential_id=credential_id)
            acknowledgement = self.state.record_pending_acknowledgement(
                credential_id,
                require_sha256(marker, "request_hash"),
                delivery["delivery_expires_at"],
            )
            active = self.state.activate_pending_certificate(
                delivery["certificate"],
                delivery["chain"],
                self.expected_identity_uri,
            )
            self._assert_active_matches_marker(active, marker)
            generation = active.generation
        else:
            if acknowledgement is None:
                raise RecoveryError(
                    "Recovered certificate is active but acknowledgement state is missing"
                )
            self._assert_ack_matches_marker(marker, acknowledgement)
            active = self.state.active_identity(self.expected_identity_uri)
            if active is None:
                raise RecoveryError("Recovered mTLS identity is not active")
            self._assert_active_matches_marker(active, marker)
            generation = active.generation

        if acknowledgement is None:
            acknowledgement = self.state.pending_acknowledgement()
        if acknowledgement is None:
            raise RecoveryError("Recovered certificate acknowledgement state is missing")
        self._assert_ack_matches_marker(marker, acknowledgement)

        acknowledged = self._acknowledge(recovery_token, credential_id)
        marker = {
            **marker,
            "acknowledged_at": format_timestamp(acknowledged["acknowledged_at"]),
            "acknowledgement_replayed": acknowledged["already_processed"],
        }
        self._write_marker(marker)
        return self._finish_acknowledged_cleanup(
            marker,
            self.state.pending_acknowledgement(),
            generation=generation,
        )

    def _finish_acknowledged_cleanup(
        self,
        marker: dict[str, Any],
        acknowledgement: Any | None,
        *,
        generation: str | None = None,
    ) -> dict[str, Any]:
        credential_id = optional_credential_id(marker.get("credential_id"))
        if credential_id is None:
            raise RecoveryError("Acknowledged recovery marker is not bound to a credential")
        acknowledged_at_raw = marker.get("acknowledged_at")
        if not isinstance(acknowledged_at_raw, str):
            raise RecoveryError("Recovery acknowledgement timestamp is missing")
        acknowledged_at = parse_timestamp(acknowledged_at_raw)
        replayed = marker.get("acknowledgement_replayed")
        if not isinstance(replayed, bool):
            raise RecoveryError("Recovery acknowledgement replay state is missing")

        active = self.state.active_identity(self.expected_identity_uri)
        if active is None:
            raise RecoveryError("Recovered mTLS identity is not active")
        self._assert_active_matches_marker(active, marker)
        if acknowledgement is not None:
            self._assert_ack_matches_marker(marker, acknowledgement)
            self.state.clear_pending_acknowledgement(credential_id)

        self._write_runtime_shadow_ready()
        safe_unlink(self.recovery_token_file)
        safe_unlink(self.pending_file)

        return {
            "ok": True,
            "node_id": self.node_id,
            "tenant_id": self.tenant_id,
            "credential_id": credential_id,
            "generation": generation or active.generation,
            "acknowledged_at": format_timestamp(acknowledged_at),
            "acknowledgement_replayed": replayed,
            "mtls_runtime_state": "SHADOW_READY",
            "agent_version": AGENT_VERSION,
        }

    def _api_json(
        self,
        recovery_token: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        expected: tuple[int, ...],
    ) -> dict[str, Any]:
        body = (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        req = request.Request(
            f"{self.api_base.rstrip('/')}/{path.lstrip('/')}",
            data=body,
            headers={
                "Authorization": f"Bearer {recovery_token}",
                "Content-Type": "application/json",
                "User-Agent": f"wavemesh-node-recovery/{AGENT_VERSION}",
            },
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            raw_problem = exc.read(MAX_RESPONSE_BYTES + 1)
            problem = parse_problem(raw_problem)
            code = safe_code(problem.get("code"))
            retryable = bool(problem.get("retryable", False))
            raise RecoveryError(
                "Recovery API rejected the request: "
                f"status={exc.code} code={code} retryable={str(retryable).lower()}"
            ) from None
        except (error.URLError, OSError) as exc:
            raise RecoveryError("Recovery API is temporarily unreachable") from exc

        if len(raw) > MAX_RESPONSE_BYTES:
            raise RecoveryError("Recovery API response exceeds the maximum size")
        if status not in expected:
            raise RecoveryError(f"Recovery API returned unexpected status {status}")
        if not raw:
            raise RecoveryError("Recovery API returned an empty response")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError("Recovery API returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise RecoveryError("Recovery API returned a non-object response")
        return decoded

    def _validate_delivery(
        self,
        response: dict[str, Any],
        expected_credential_id: str | None = None,
    ) -> dict[str, Any]:
        credential_id = require_credential_id(response, "credential_id")
        if expected_credential_id is not None and credential_id != expected_credential_id:
            raise RecoveryError("Recovery API returned another certificate delivery")
        if response.get("lifecycle_status") != "PENDING_ACKNOWLEDGEMENT":
            raise RecoveryError("Recovery certificate lifecycle state is invalid")
        certificate = require_bounded_string(response, "certificate", MAX_PEM_BYTES)
        chain = require_bounded_string(response, "chain", MAX_PEM_BYTES)
        not_before = parse_timestamp(require_bounded_string(response, "not_before", 64))
        expires_at = parse_timestamp(require_bounded_string(response, "expires_at", 64))
        delivery_expires_at = parse_timestamp(
            require_bounded_string(response, "delivery_expires_at", 64)
        )
        now = datetime.now(timezone.utc)
        if expires_at <= now:
            raise RecoveryError("Recovered certificate is already expired")
        if delivery_expires_at <= now or delivery_expires_at > expires_at:
            raise RecoveryError("Recovered certificate delivery expiry is invalid")
        if not_before >= expires_at:
            raise RecoveryError("Recovered certificate validity window is invalid")
        if response.get("previous_valid_until") is not None:
            raise RecoveryError("Break-glass recovery must not retain an overlap window")
        already_processed = response.get("already_processed")
        if not isinstance(already_processed, bool):
            raise RecoveryError("Recovery replay metadata is invalid")
        recovery_reason = response.get("recovery_reason")
        if recovery_reason is not None and recovery_reason not in {"LOST_KEY", "COMPROMISED_KEY"}:
            raise RecoveryError("Recovery reason metadata is invalid")
        return {
            "credential_id": credential_id,
            "certificate": certificate,
            "chain": chain,
            "expires_at": expires_at,
            "delivery_expires_at": delivery_expires_at,
            "already_processed": already_processed,
        }

    def _acknowledge(
        self,
        recovery_token: str,
        credential_id: str,
    ) -> dict[str, Any]:
        response = self._api_json(
            recovery_token,
            "POST",
            (
                "internal/v1/nodes/recover/certificates/"
                f"{credential_id}/acknowledge"
            ),
            {},
            expected=(200, 201),
        )
        if require_credential_id(response, "credential_id") != credential_id:
            raise RecoveryError("Recovery API acknowledged another certificate delivery")
        if response.get("lifecycle_status") != "ACKNOWLEDGED":
            raise RecoveryError("Recovery acknowledgement lifecycle state is invalid")
        already_processed = response.get("already_processed")
        if not isinstance(already_processed, bool):
            raise RecoveryError("Recovery acknowledgement replay metadata is invalid")
        return {
            "acknowledged_at": parse_timestamp(
                require_bounded_string(response, "acknowledged_at", 64)
            ),
            "already_processed": already_processed,
        }

    def _new_marker(self, pending: Any) -> dict[str, Any]:
        marker = {
            "version": MARKER_VERSION,
            "node_id": self.node_id,
            "tenant_id": self.tenant_id,
            "request_hash": require_sha256_value(
                pending.request_hash, "Pending recovery request hash"
            ),
            "public_key_hash": require_sha256_value(
                pending.public_key_hash, "Pending recovery public key hash"
            ),
            "created_at": require_timestamp_string(
                pending.created_at, "Pending recovery creation timestamp"
            ),
            "credential_id": None,
            "acknowledged_at": None,
            "acknowledgement_replayed": None,
        }
        return marker

    def _bind_credential(
        self,
        marker: dict[str, Any],
        credential_id: str,
    ) -> dict[str, Any]:
        existing = optional_credential_id(marker.get("credential_id"))
        if existing is not None and existing != credential_id:
            raise RecoveryError("Recovery marker is already bound to another credential")
        return {
            **marker,
            "credential_id": credential_id,
        }

    def _load_marker(self, required: bool) -> dict[str, Any] | None:
        if not self.pending_file.exists():
            if self.pending_file.is_symlink():
                raise RecoveryError("Recovery pending marker path is unsafe")
            if required:
                raise RecoveryError("Recovery pending marker is missing")
            return None
        ensure_restricted_regular_file(self.pending_file, "Recovery pending marker")
        try:
            value = json.loads(self.pending_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError("Recovery pending marker is unreadable") from exc
        if not isinstance(value, dict) or value.get("version") != MARKER_VERSION:
            raise RecoveryError("Recovery pending marker is incompatible or invalid")
        if require_scope_id(value.get("node_id"), "Recovery marker Node ID") != self.node_id:
            raise RecoveryError("Recovery pending marker Node ID does not match")
        if (
            require_scope_id(value.get("tenant_id"), "Recovery marker tenant ID")
            != self.tenant_id
        ):
            raise RecoveryError("Recovery pending marker tenant ID does not match")
        require_sha256(value, "request_hash")
        require_sha256(value, "public_key_hash")
        parse_timestamp(require_bounded_string(value, "created_at", 64))
        optional_credential_id(value.get("credential_id"))
        acknowledged_at = value.get("acknowledged_at")
        replayed = value.get("acknowledgement_replayed")
        if acknowledged_at is not None:
            if not isinstance(acknowledged_at, str):
                raise RecoveryError("Recovery marker acknowledgement timestamp is invalid")
            parse_timestamp(acknowledged_at)
            if not isinstance(replayed, bool):
                raise RecoveryError("Recovery marker acknowledgement replay state is invalid")
        elif replayed is not None:
            raise RecoveryError("Recovery marker acknowledgement state is invalid")
        return value

    def _write_marker(self, marker: dict[str, Any]) -> None:
        atomic_write_json(self.pending_file, marker, PRIVATE_FILE_MODE)

    def _validate_local_state(
        self,
        marker: dict[str, Any],
        pending_present: bool,
        acknowledgement: Any | None,
    ) -> None:
        credential_id = optional_credential_id(marker.get("credential_id"))
        acknowledged_at = marker.get("acknowledged_at")
        if acknowledged_at is not None:
            if credential_id is None:
                raise RecoveryError(
                    "Acknowledged recovery marker is not bound to a credential"
                )
            if pending_present:
                raise RecoveryError(
                    "Acknowledged recovery still has pending CSR/private key material"
                )
            if acknowledgement is not None:
                self._assert_ack_matches_marker(marker, acknowledgement)
            active = self.state.active_identity(self.expected_identity_uri)
            if active is None:
                raise RecoveryError("Acknowledged recovered mTLS identity is not active")
            self._assert_active_matches_marker(active, marker)
            return
        if credential_id is None:
            if acknowledgement is not None:
                raise RecoveryError(
                    "Recovery acknowledgement exists before credential binding"
                )
            if not pending_present:
                raise RecoveryError(
                    "Recovery marker exists but the pending CSR/private key is missing"
                )
            return
        if acknowledgement is not None:
            self._assert_ack_matches_marker(marker, acknowledgement)
        if not pending_present and acknowledgement is None:
            raise RecoveryError(
                "Recovered certificate state is incomplete: acknowledgement marker is missing"
            )

    def _assert_pending_matches_marker(
        self,
        marker: dict[str, Any],
        pending: Any,
    ) -> None:
        if (
            require_sha256_value(pending.request_hash, "Pending request hash")
            != require_sha256(marker, "request_hash")
            or require_sha256_value(pending.public_key_hash, "Pending public key hash")
            != require_sha256(marker, "public_key_hash")
        ):
            raise RecoveryError(
                "Persisted recovery marker does not match the pending CSR/private key"
            )

    def _assert_ack_matches_marker(
        self,
        marker: dict[str, Any],
        acknowledgement: Any,
    ) -> None:
        credential_id = optional_credential_id(marker.get("credential_id"))
        if credential_id is None:
            raise RecoveryError("Recovery marker is not bound to a credential")
        if (
            acknowledgement.credential_id != credential_id
            or acknowledgement.request_hash != require_sha256(marker, "request_hash")
        ):
            raise RecoveryError("Recovery acknowledgement does not match the pending recovery")

    def _assert_active_matches_marker(
        self,
        active: Any,
        marker: dict[str, Any],
    ) -> None:
        active_request_hash = self.state.active_request_hash(active)
        if active_request_hash != require_sha256(marker, "request_hash"):
            raise RecoveryError("Active mTLS identity does not match the recovery request")

    def _pending_request_present(self) -> bool:
        paths = (
            self.state.pending_key,
            self.state.pending_csr,
            self.state.pending_metadata,
        )
        existing = [path.exists() for path in paths]
        if any(existing) and not all(existing):
            raise RecoveryError(
                "Partial pending mTLS identity exists; refusing to regenerate"
            )
        return all(existing)

    def _write_runtime_shadow_ready(self) -> None:
        atomic_write_json(
            self.runtime_file,
            {
                "code": None,
                "retry_at": None,
                "retry_attempts": 0,
                "state": "SHADOW_READY",
                "version": 1,
            },
            PRIVATE_FILE_MODE,
        )

    def _assert_no_legacy_state(self) -> None:
        if self.legacy_accepted_file.exists() or self.legacy_accepted_file.is_symlink():
            raise RecoveryError(
                "Legacy temporary-bearer recovery state exists; refusing direct recovery"
            )

    def _runtime_state(self) -> str:
        if not self.runtime_file.is_file() or self.runtime_file.is_symlink():
            return "MISSING"
        try:
            value = json.loads(self.runtime_file.read_text(encoding="utf-8"))
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


def require_environment(value: Any) -> str:
    normalized = str(value or "").strip()
    if not SAFE_ENVIRONMENT_PATTERN.fullmatch(normalized):
        raise RecoveryError("WAVEMESH_AGENT_MTLS_ENVIRONMENT is invalid")
    return normalized


def require_scope_id(value: Any, label: str) -> str:
    item = str(value or "")
    if not SAFE_SCOPE_ID_PATTERN.fullmatch(item):
        raise RecoveryError(f"{label} is invalid")
    return item


def require_credential_id(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not SAFE_CREDENTIAL_ID_PATTERN.fullmatch(item):
        raise RecoveryError(f"Recovery response has invalid {key}")
    return item


def optional_credential_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not SAFE_CREDENTIAL_ID_PATTERN.fullmatch(value):
        raise RecoveryError("Recovery marker credential ID is invalid")
    return value


def require_bounded_string(
    value: dict[str, Any],
    key: str,
    maximum_bytes: int,
) -> str:
    item = value.get(key)
    if (
        not isinstance(item, str)
        or not item
        or len(item.encode("utf-8")) > maximum_bytes
    ):
        raise RecoveryError(f"Recovery response is missing or invalid: {key}")
    return item


def require_sha256(value: dict[str, Any], key: str) -> str:
    return require_sha256_value(value.get(key), f"Recovery marker field {key}")


def require_sha256_value(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise RecoveryError(f"{label} is invalid")
    return value


def require_timestamp_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RecoveryError(f"{label} is invalid")
    parsed = parse_timestamp(value)
    return format_timestamp(parsed)


def parse_problem(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_RESPONSE_BYTES:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    nested = value.get("error")
    return nested if isinstance(nested, dict) else value


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
    parser = argparse.ArgumentParser(
        description="WaveMesh direct CSR node certificate recovery"
    )
    parser.add_argument("command", choices=("check", "apply"))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_RECOVERY_TOKEN_FILE)
    parser.add_argument("--external-node-id", required=True)
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=30,
        choices=range(5, 121),
        metavar="5..120",
    )
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
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RecoveryError as exc:
        raise SystemExit(f"node recovery failed: {exc}") from None
