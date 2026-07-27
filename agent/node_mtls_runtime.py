#!/usr/bin/env python3
"""Shadow-first mTLS state machine for the observe-only Node Agent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import os
from pathlib import Path
import re
from typing import Any

try:
    from node_mtls_client import (
        CertificateLifecycleResult,
        MtlsApiError,
        MtlsClientConfig,
        MtlsClientError,
        NodeCertificateLifecycleClient,
        NodeMtlsTransport,
        certificate_rotation_due,
        parse_certificate_expiry,
    )
    from node_mtls_state import (
        NodeMtlsState,
        PRIVATE_FILE_MODE,
        atomic_write_json,
        read_json_object,
    )
except ImportError:  # pragma: no cover - package-style import in tests/tools
    from .node_mtls_client import (
        CertificateLifecycleResult,
        MtlsApiError,
        MtlsClientConfig,
        MtlsClientError,
        NodeCertificateLifecycleClient,
        NodeMtlsTransport,
        certificate_rotation_due,
        parse_certificate_expiry,
    )
    from .node_mtls_state import (
        NodeMtlsState,
        PRIVATE_FILE_MODE,
        atomic_write_json,
        read_json_object,
    )

MTLS_MODES = ("disabled", "shadow")
SAFE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")


class MtlsRuntimeError(RuntimeError):
    pass


class MtlsAgentState(str, Enum):
    BEARER_ONLY = "BEARER_ONLY"
    ENROLLING = "ENROLLING"
    SHADOW_READY = "SHADOW_READY"
    SHADOW_ACTIVE = "SHADOW_ACTIVE"
    ROTATING = "ROTATING"
    FALLBACK = "FALLBACK"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class MtlsRuntimeConfig:
    mode: str
    bearer_api_base: str
    mtls_api_base: str | None
    node_id: str
    tenant_id: str
    environment: str
    bearer_token: str
    state_root: Path
    server_ca_file: Path | None = None
    request_timeout_seconds: int = 30
    rotate_before_seconds: int = 6 * 60 * 60
    retry_base_seconds: int = 30
    retry_max_seconds: int = 15 * 60
    retry_max_attempts: int = 8
    retry_jitter_seconds: int = 0

    def __post_init__(self) -> None:
        if self.mode not in MTLS_MODES:
            raise MtlsRuntimeError("Unsupported Agent mTLS mode")
        if self.mode == "disabled":
            return
        if not self.mtls_api_base or not self.mtls_api_base.startswith("https://"):
            raise MtlsRuntimeError("Shadow mTLS API base must use HTTPS")
        if not 15 * 60 <= self.rotate_before_seconds <= 3 * 24 * 60 * 60:
            raise MtlsRuntimeError("Agent mTLS rotation threshold is invalid")
        if not 5 <= self.retry_base_seconds <= 300:
            raise MtlsRuntimeError("Agent mTLS retry base is invalid")
        if not self.retry_base_seconds <= self.retry_max_seconds <= 3600:
            raise MtlsRuntimeError("Agent mTLS retry maximum is invalid")
        if not 1 <= self.retry_max_attempts <= 32:
            raise MtlsRuntimeError("Agent mTLS retry attempt limit is invalid")
        if not 0 <= self.retry_jitter_seconds <= 300:
            raise MtlsRuntimeError("Agent mTLS retry jitter is invalid")
        MtlsClientConfig(
            api_base=self.bearer_api_base,
            node_id=self.node_id,
            tenant_id=self.tenant_id,
            environment=self.environment,
            auth_mode="bearer",
            bearer_token=self.bearer_token,
            request_timeout_seconds=self.request_timeout_seconds,
            state_root=self.state_root,
        )
        MtlsClientConfig(
            api_base=self.mtls_api_base,
            node_id=self.node_id,
            tenant_id=self.tenant_id,
            environment=self.environment,
            auth_mode="mtls",
            bearer_token=None,
            request_timeout_seconds=self.request_timeout_seconds,
            state_root=self.state_root,
            server_ca_file=self.server_ca_file,
        )

    @property
    def expected_identity_uri(self) -> str:
        return (
            f"spiffe://wavevpn/{self.environment}/tenant/"
            f"{self.tenant_id}/node/{self.node_id}"
        )


@dataclass(frozen=True)
class MtlsRuntimeStatus:
    state: MtlsAgentState
    retry_attempts: int
    retry_at: datetime | None
    code: str | None

    def capability(self) -> dict[str, Any]:
        return {
            "mode": "shadow" if self.state != MtlsAgentState.BEARER_ONLY else "disabled",
            "state": self.state.value,
            "retry_attempts": self.retry_attempts,
            "retry_at": format_timestamp(self.retry_at) if self.retry_at else None,
            "code": self.code,
        }


class NodeMtlsRuntime:
    """Orchestrate lifecycle work without owning the bearer Agent loop."""

    def __init__(
        self,
        config: MtlsRuntimeConfig,
        state: NodeMtlsState | None = None,
    ) -> None:
        self.config = config
        self.state = state or NodeMtlsState(config.state_root)
        self.runtime_path = config.state_root / "runtime.json"
        self.runtime = (
            self._default_runtime()
            if config.mode == "disabled"
            else self._load_runtime()
        )

    def status(self) -> MtlsRuntimeStatus:
        if self.config.mode == "disabled":
            return MtlsRuntimeStatus(MtlsAgentState.BEARER_ONLY, 0, None, None)
        state = parse_state(self.runtime.get("state"))
        attempts = bounded_int(self.runtime.get("retry_attempts"), 0, self.config.retry_max_attempts)
        retry_at = optional_timestamp(self.runtime.get("retry_at"))
        code = optional_code(self.runtime.get("code"))
        return MtlsRuntimeStatus(state, attempts, retry_at, code)

    def lifecycle_cycle(
        self,
        agent_version: str,
        now: datetime | None = None,
    ) -> MtlsRuntimeStatus:
        current = normalize_now(now)
        if self.config.mode == "disabled":
            return self.status()
        if not self._retry_due(current):
            return self.status()

        try:
            active = self.state.active_identity(self._expected_identity_uri())
            active_expiry = parse_certificate_expiry(active) if active else None
            pending_ack = self.state.pending_acknowledgement()

            if pending_ack is not None:
                if pending_ack.delivery_expires_at <= current:
                    raise MtlsRuntimeError("Pending certificate delivery expired before acknowledgement")
                if self.state.active_request_hash(active) != pending_ack.request_hash:
                    self._transition(MtlsAgentState.ENROLLING)
                    lifecycle = self._bearer_lifecycle_client()
                    lifecycle.retrieve(pending_ack.credential_id)
                self._bearer_lifecycle_client().acknowledge(pending_ack.credential_id)
                self._clear_retry(MtlsAgentState.SHADOW_READY)
                return self.status()

            if active is None or active_expiry is None or active_expiry <= current:
                self._transition(MtlsAgentState.ENROLLING)
                result = self._bearer_lifecycle_client().issue_or_rotate(agent_version)
                self._bearer_lifecycle_client().acknowledge(result.credential_id)
                self._clear_retry(MtlsAgentState.SHADOW_READY)
                return self.status()

            if not certificate_rotation_due(
                active_expiry,
                self.config.rotate_before_seconds,
                current,
            ):
                current_state = self.status().state
                stable_state = (
                    MtlsAgentState.SHADOW_ACTIVE
                    if current_state == MtlsAgentState.SHADOW_ACTIVE
                    else MtlsAgentState.SHADOW_READY
                )
                self._clear_retry(stable_state)
                return self.status()

            self._transition(MtlsAgentState.ROTATING)
            result = self._mtls_lifecycle_client().issue_or_rotate(agent_version)
            self._bearer_lifecycle_client().acknowledge(result.credential_id)
            self._clear_retry(MtlsAgentState.SHADOW_READY)
            return self.status()
        except MtlsApiError as exc:
            self._record_failure(exc.code, exc.retryable, current)
        except (MtlsClientError, MtlsRuntimeError) as exc:
            self._record_failure(type(exc).__name__.upper(), False, current)
        except Exception as exc:  # noqa: BLE001 - isolate mTLS from bearer health
            self._record_failure(type(exc).__name__.upper(), False, current)
        return self.status()

    def shadow_heartbeat(
        self,
        payload: dict[str, Any],
        now: datetime | None = None,
    ) -> MtlsRuntimeStatus:
        current = normalize_now(now)
        if self.config.mode == "disabled" or not self._retry_due(current):
            return self.status()
        status = self.status()
        if status.state in {
            MtlsAgentState.ENROLLING,
            MtlsAgentState.ROTATING,
            MtlsAgentState.FALLBACK,
            MtlsAgentState.BLOCKED,
        }:
            return status

        try:
            active = self.state.active_identity(self._expected_identity_uri())
            if active is None or parse_certificate_expiry(active) <= current:
                raise MtlsRuntimeError("Active mTLS identity is unavailable for shadow heartbeat")
            self._mtls_transport().api_json(
                "POST",
                f"internal/v1/nodes/{self.config.node_id}/heartbeat",
                payload,
                expected=(204,),
            )
            self._clear_retry(MtlsAgentState.SHADOW_ACTIVE)
        except MtlsApiError as exc:
            self._record_failure(exc.code, exc.retryable, current)
        except (MtlsClientError, MtlsRuntimeError) as exc:
            self._record_failure(type(exc).__name__.upper(), False, current)
        except Exception as exc:  # noqa: BLE001 - isolate mTLS from bearer health
            self._record_failure(type(exc).__name__.upper(), False, current)
        return self.status()

    def _bearer_lifecycle_client(self) -> NodeCertificateLifecycleClient:
        client_config = MtlsClientConfig(
            api_base=self.config.bearer_api_base,
            node_id=self.config.node_id,
            tenant_id=self.config.tenant_id,
            environment=self.config.environment,
            auth_mode="bearer",
            bearer_token=self.config.bearer_token,
            request_timeout_seconds=self.config.request_timeout_seconds,
            state_root=self.config.state_root,
        )
        return NodeCertificateLifecycleClient(client_config, state=self.state)

    def _mtls_lifecycle_client(self) -> NodeCertificateLifecycleClient:
        client_config = self._mtls_client_config()
        return NodeCertificateLifecycleClient(client_config, state=self.state)

    def _mtls_transport(self) -> NodeMtlsTransport:
        return NodeMtlsTransport(self._mtls_client_config(), state=self.state)

    def _mtls_client_config(self) -> MtlsClientConfig:
        if not self.config.mtls_api_base:
            raise MtlsRuntimeError("Shadow mTLS API base is unavailable")
        return MtlsClientConfig(
            api_base=self.config.mtls_api_base,
            node_id=self.config.node_id,
            tenant_id=self.config.tenant_id,
            environment=self.config.environment,
            auth_mode="mtls",
            bearer_token=None,
            request_timeout_seconds=self.config.request_timeout_seconds,
            state_root=self.config.state_root,
            server_ca_file=self.config.server_ca_file,
        )

    def _expected_identity_uri(self) -> str:
        return self.config.expected_identity_uri

    def _retry_due(self, now: datetime) -> bool:
        status = self.status()
        if status.state == MtlsAgentState.BLOCKED:
            return False
        return status.retry_at is None or now >= status.retry_at

    def _record_failure(self, code: str, retryable: bool, now: datetime) -> None:
        safe_code = sanitize_code(code)
        previous = self.status().retry_attempts
        attempts = previous + 1
        if not retryable or attempts >= self.config.retry_max_attempts:
            self._write_runtime(
                MtlsAgentState.BLOCKED,
                attempts=min(attempts, self.config.retry_max_attempts),
                retry_at=None,
                code=safe_code,
            )
            return

        delay = min(
            self.config.retry_max_seconds,
            self.config.retry_base_seconds * (2 ** min(20, attempts - 1)),
        )
        delay = min(
            self.config.retry_max_seconds,
            delay + deterministic_retry_jitter(
                self.config.node_id,
                attempts,
                self.config.retry_jitter_seconds,
            ),
        )
        self._write_runtime(
            MtlsAgentState.FALLBACK,
            attempts=attempts,
            retry_at=now + timedelta(seconds=delay),
            code=safe_code,
        )

    def _transition(self, state: MtlsAgentState) -> None:
        status = self.status()
        self._write_runtime(
            state,
            attempts=status.retry_attempts,
            retry_at=status.retry_at,
            code=status.code,
        )

    def _clear_retry(self, state: MtlsAgentState) -> None:
        self._write_runtime(state, attempts=0, retry_at=None, code=None)

    def _write_runtime(
        self,
        state: MtlsAgentState,
        *,
        attempts: int,
        retry_at: datetime | None,
        code: str | None,
    ) -> None:
        value = {
            "code": code,
            "retry_at": format_timestamp(retry_at) if retry_at else None,
            "retry_attempts": attempts,
            "state": state.value,
            "version": 1,
        }
        if value == self.runtime:
            return
        if self.config.state_root.is_symlink():
            raise MtlsRuntimeError("Agent mTLS state root must not be a symlink")
        self.config.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.state_root, 0o700)
        atomic_write_json(self.runtime_path, value, PRIVATE_FILE_MODE)
        self.runtime = value

    def _load_runtime(self) -> dict[str, Any]:
        if not self.runtime_path.exists():
            return self._default_runtime()
        if self.runtime_path.is_symlink() or not self.runtime_path.is_file():
            raise MtlsRuntimeError("Agent mTLS runtime path is unsafe")
        if os.name != "nt" and self.runtime_path.stat().st_mode & 0o777 != PRIVATE_FILE_MODE:
            raise MtlsRuntimeError("Agent mTLS runtime permissions are invalid")
        value = read_json_object(self.runtime_path)
        if value.get("version") != 1:
            raise MtlsRuntimeError("Agent mTLS runtime version is invalid")
        parse_state(value.get("state"))
        bounded_int(value.get("retry_attempts"), 0, self.config.retry_max_attempts)
        optional_timestamp(value.get("retry_at"))
        optional_code(value.get("code"))
        return value

    @staticmethod
    def _default_runtime() -> dict[str, Any]:
        return {
            "code": None,
            "retry_at": None,
            "retry_attempts": 0,
            "state": MtlsAgentState.BEARER_ONLY.value,
            "version": 1,
        }


def parse_state(value: Any) -> MtlsAgentState:
    try:
        return MtlsAgentState(str(value))
    except ValueError as exc:
        raise MtlsRuntimeError("Agent mTLS state is invalid") from exc


def optional_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MtlsRuntimeError("Agent mTLS retry timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MtlsRuntimeError("Agent mTLS retry timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise MtlsRuntimeError("Agent mTLS retry timestamp requires timezone")
    return parsed.astimezone(timezone.utc)


def optional_code(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not SAFE_CODE_PATTERN.fullmatch(value):
        raise MtlsRuntimeError("Agent mTLS status code is invalid")
    return value


def sanitize_code(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9_]", "_", str(value).upper())[:96]
    if not normalized or not normalized[0].isalpha():
        return "MTLS_INTERNAL_ERROR"
    return normalized


def bounded_int(value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise MtlsRuntimeError("Agent mTLS retry count is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MtlsRuntimeError("Agent mTLS retry count is invalid") from exc
    if not minimum <= parsed <= maximum:
        raise MtlsRuntimeError("Agent mTLS retry count is out of range")
    return parsed


def deterministic_retry_jitter(node_id: str, attempt: int, maximum: int) -> int:
    if maximum <= 0:
        return 0
    material = f"mtls-retry\0{node_id}\0{attempt}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % (maximum + 1)


def normalize_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise MtlsRuntimeError("Agent mTLS clock value requires timezone")
    return current.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
