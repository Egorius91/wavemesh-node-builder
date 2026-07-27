#!/usr/bin/env python3
"""Observe-only Node Agent mTLS network and certificate lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
from typing import Any, Callable, Protocol
from urllib import error, request

try:
    from node_mtls_state import ActiveIdentity, NodeMtlsState
except ImportError:  # pragma: no cover - package-style import in tests/tools
    from .node_mtls_state import ActiveIdentity, NodeMtlsState

AUTH_MODES = ("bearer", "bootstrap-mtls", "mtls")
MAX_RESPONSE_BYTES = 256 * 1024


class MtlsClientError(RuntimeError):
    pass


class MtlsApiError(MtlsClientError):
    def __init__(self, status: int, code: str, retryable: bool, message: str = "") -> None:
        super().__init__(f"HTTP {status} {code}: {message}".strip())
        self.status = status
        self.code = code
        self.retryable = retryable


class ResponseLike(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> "ResponseLike": ...

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...


OpenUrl = Callable[..., ResponseLike]


@dataclass(frozen=True)
class MtlsClientConfig:
    api_base: str
    node_id: str
    tenant_id: str
    environment: str
    auth_mode: str = "bearer"
    bearer_token: str | None = None
    request_timeout_seconds: int = 30
    state_root: Path = Path("/etc/wavemesh-agent/tls")
    server_ca_file: Path | None = None

    def __post_init__(self) -> None:
        if self.auth_mode not in AUTH_MODES:
            raise MtlsClientError("Unsupported Node Agent authentication mode")
        if not self.api_base.startswith("https://"):
            raise MtlsClientError("Node Agent API base must use HTTPS")
        if not _safe_id(self.node_id) or not _safe_id(self.tenant_id):
            raise MtlsClientError("Node or tenant ID is invalid")
        if not _safe_environment(self.environment):
            raise MtlsClientError("Node mTLS environment is invalid")
        if self.auth_mode in {"bearer", "bootstrap-mtls"} and not _valid_bearer(self.bearer_token):
            raise MtlsClientError("Bearer/bootstrap mode requires a valid Node bearer token")
        if not 5 <= self.request_timeout_seconds <= 120:
            raise MtlsClientError("Node Agent request timeout is invalid")
        if self.server_ca_file is not None:
            if not self.server_ca_file.is_absolute():
                raise MtlsClientError("Node mTLS server CA path must be absolute")
            if self.server_ca_file.is_symlink() or not self.server_ca_file.is_file():
                raise MtlsClientError("Node mTLS server CA path is unsafe")

    @property
    def expected_identity_uri(self) -> str:
        return (
            f"spiffe://wavevpn/{self.environment}/tenant/"
            f"{self.tenant_id}/node/{self.node_id}"
        )


@dataclass(frozen=True)
class CertificateLifecycleResult:
    credential_id: str
    expires_at: datetime
    delivery_expires_at: datetime
    previous_valid_until: datetime | None
    already_processed: bool
    generation: str


class NodeMtlsTransport:
    """HTTP transport with explicit bearer/bootstrap/mTLS mode selection.

    No request in ``mtls`` mode can fall back to bearer. ``bootstrap-mtls`` uses
    bearer until a locally validated identity is active, then switches all
    requests to mTLS.
    """

    def __init__(
        self,
        config: MtlsClientConfig,
        state: NodeMtlsState | None = None,
        opener: OpenUrl | None = None,
    ) -> None:
        self.config = config
        self.state = state or NodeMtlsState(config.state_root)
        self.opener = opener or request.urlopen

    def api_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        expected: tuple[int, ...],
        headers: dict[str, str] | None = None,
        *,
        certificate_request: bool = False,
    ) -> dict[str, Any]:
        auth_headers, ssl_context = self._authentication(certificate_request=certificate_request)
        body = (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        request_headers = {
            **auth_headers,
            "X-WaveVPN-Tenant-Id": self.config.tenant_id,
            "Content-Type": "application/json",
            "User-Agent": "wavemesh-node-agent/mtls-lifecycle",
        }
        if headers:
            request_headers.update(headers)
        req = request.Request(
            f"{self.config.api_base.rstrip('/')}/{path.lstrip('/')}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with self.opener(
                req,
                timeout=self.config.request_timeout_seconds,
                context=ssl_context,
            ) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            problem = _parse_problem(exc.read(MAX_RESPONSE_BYTES + 1))
            raise MtlsApiError(
                exc.code,
                str(problem.get("code") or "HTTP_ERROR"),
                bool(problem.get("retryable", False)),
                str(problem.get("message") or ""),
            ) from None
        except (error.URLError, ssl.SSLError, OSError) as exc:
            raise MtlsApiError(0, "NETWORK_OR_TLS_ERROR", True, _safe_error(str(exc))) from None

        if len(raw) > MAX_RESPONSE_BYTES:
            raise MtlsClientError("SaaS response exceeds the maximum size")
        if status not in expected:
            raise MtlsApiError(status, "UNEXPECTED_STATUS", True)
        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MtlsClientError("SaaS returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise MtlsClientError("SaaS returned a non-object JSON response")
        return decoded

    def _authentication(self, *, certificate_request: bool) -> tuple[dict[str, str], ssl.SSLContext]:
        del certificate_request  # mode and active state fully determine transport authentication
        if self.config.auth_mode == "bearer":
            return self._bearer_authentication()
        active = self.state.active_identity(self.config.expected_identity_uri)
        if self.config.auth_mode == "bootstrap-mtls" and active is None:
            return self._bearer_authentication()
        if active is None:
            raise MtlsClientError("mTLS authentication requires an active local identity")
        return {}, build_mtls_ssl_context(active, self.config.server_ca_file)

    def _bearer_authentication(self) -> tuple[dict[str, str], ssl.SSLContext]:
        if not _valid_bearer(self.config.bearer_token):
            raise MtlsClientError("A valid bearer token is unavailable")
        return (
            {"Authorization": f"Bearer {self.config.bearer_token}"},
            ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH),
        )


class NodeCertificateLifecycleClient:
    def __init__(
        self,
        config: MtlsClientConfig,
        state: NodeMtlsState | None = None,
        transport: NodeMtlsTransport | None = None,
    ) -> None:
        self.config = config
        self.state = state or NodeMtlsState(config.state_root)
        self.transport = transport or NodeMtlsTransport(config, self.state)

    def issue_or_rotate(self, agent_version: str) -> CertificateLifecycleResult:
        pending = self.state.prepare_pending_request()
        idempotency_key = certificate_idempotency_key(
            self.config.node_id,
            pending.request_hash,
        )
        response = self.transport.api_json(
            "POST",
            f"internal/v1/nodes/{self.config.node_id}/certificates",
            {
                "csr": pending.csr_pem,
                "agent_version": _safe_agent_version(agent_version),
            },
            expected=(201,),
            headers={"Idempotency-Key": idempotency_key},
            certificate_request=True,
        )
        return self._activate_delivery(response, pending.request_hash)

    def retrieve(self, credential_id: str) -> CertificateLifecycleResult:
        pending = self.state.prepare_pending_request()
        response = self.transport.api_json(
            "GET",
            f"internal/v1/nodes/{self.config.node_id}/certificates/{_safe_credential_id(credential_id)}",
            None,
            expected=(200,),
            certificate_request=True,
        )
        response_credential_id = _required_safe_id(response, "credential_id")
        if response_credential_id != credential_id:
            raise MtlsClientError("SaaS returned another certificate delivery")
        return self._activate_delivery(response, pending.request_hash)

    def acknowledge(self, credential_id: str) -> datetime:
        response = self.transport.api_json(
            "POST",
            (
                f"internal/v1/nodes/{self.config.node_id}/certificates/"
                f"{_safe_credential_id(credential_id)}/acknowledge"
            ),
            {},
            expected=(200, 201),
            certificate_request=True,
        )
        if _safe_credential_id(_required_safe_id(response, "credential_id")) != credential_id:
            raise MtlsClientError("SaaS acknowledged another certificate delivery")
        if response.get("lifecycle_status") != "ACKNOWLEDGED":
            raise MtlsClientError("SaaS returned an invalid acknowledgement state")
        acknowledged_at = _required_timestamp(response, "acknowledged_at")
        already_processed = response.get("already_processed")
        if not isinstance(already_processed, bool):
            raise MtlsClientError("SaaS acknowledgement replay metadata is invalid")
        self.state.clear_pending_acknowledgement(credential_id)
        return acknowledged_at

    def _activate_delivery(
        self,
        response: dict[str, Any],
        request_hash: str,
    ) -> CertificateLifecycleResult:
        certificate = _required_string(response, "certificate", 128 * 1024)
        chain = _required_string(response, "chain", 128 * 1024)
        credential_id = _safe_credential_id(_required_safe_id(response, "credential_id"))
        expires_at = _required_timestamp(response, "expires_at")
        delivery_expires_at = _required_timestamp(response, "delivery_expires_at")
        previous_valid_until = _optional_timestamp(response, "previous_valid_until")
        if response.get("lifecycle_status") != "PENDING_ACKNOWLEDGEMENT":
            raise MtlsClientError("SaaS certificate response has invalid lifecycle state")
        already_processed = response.get("already_processed")
        if not isinstance(already_processed, bool):
            raise MtlsClientError("SaaS certificate response has invalid replay metadata")
        if delivery_expires_at > expires_at:
            raise MtlsClientError("SaaS certificate delivery expiry is invalid")

        self.state.record_pending_acknowledgement(
            credential_id,
            request_hash,
            delivery_expires_at,
        )
        active = self.state.activate_pending_certificate(
            certificate,
            chain,
            self.config.expected_identity_uri,
        )
        return CertificateLifecycleResult(
            credential_id=credential_id,
            expires_at=expires_at,
            delivery_expires_at=delivery_expires_at,
            previous_valid_until=previous_valid_until,
            already_processed=already_processed,
            generation=active.generation,
        )


def build_mtls_ssl_context(
    active: ActiveIdentity,
    server_ca_file: Path | None = None,
) -> ssl.SSLContext:
    context = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH,
        cafile=str(server_ca_file) if server_ca_file is not None else None,
    )
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(
        certfile=str(active.certificate_chain),
        keyfile=str(active.private_key),
    )
    return context


def certificate_idempotency_key(node_id: str, request_hash: str) -> str:
    if not _safe_id(node_id) or not _sha256(request_hash):
        raise MtlsClientError("Certificate request identity metadata is invalid")
    opaque = hashlib.sha256(f"{node_id}\0{request_hash}".encode("utf-8")).hexdigest()
    return f"node-certificate-{opaque}"


def certificate_rotation_due(
    expires_at: datetime,
    rotate_before_seconds: int,
    now: datetime | None = None,
) -> bool:
    if expires_at.tzinfo is None:
        raise MtlsClientError("Certificate expiry must include a timezone")
    if not 15 * 60 <= rotate_before_seconds <= 3 * 24 * 60 * 60:
        raise MtlsClientError("Certificate rotation threshold is invalid")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    due_at = expires_at.astimezone(timezone.utc) - timedelta(seconds=rotate_before_seconds)
    return current >= due_at


def parse_certificate_expiry(active: ActiveIdentity, openssl_binary: str = "openssl") -> datetime:
    completed = subprocess.run(
        [openssl_binary, "x509", "-in", str(active.certificate), "-noout", "-enddate"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
    )
    if completed.returncode != 0:
        raise MtlsClientError("Active certificate expiry could not be read")
    line = completed.stdout.strip()
    if not line.startswith("notAfter="):
        raise MtlsClientError("Active certificate expiry output is invalid")
    try:
        return datetime.strptime(
            line[len("notAfter=") :],
            "%b %d %H:%M:%S %Y %Z",
        ).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise MtlsClientError("Active certificate expiry timestamp is invalid") from exc


def sanitized_lifecycle_metadata(result: CertificateLifecycleResult) -> dict[str, Any]:
    return {
        "credential_id": result.credential_id,
        "expires_at": _format_timestamp(result.expires_at),
        "delivery_expires_at": _format_timestamp(result.delivery_expires_at),
        "previous_valid_until": (
            _format_timestamp(result.previous_valid_until)
            if result.previous_valid_until
            else None
        ),
        "already_processed": result.already_processed,
        "generation": result.generation,
    }


def _parse_problem(raw: bytes) -> dict[str, Any]:
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


def _required_string(value: dict[str, Any], key: str, maximum: int) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or len(item.encode("utf-8")) > maximum:
        raise MtlsClientError(f"SaaS certificate response is missing {key}")
    return item


def _required_safe_id(value: dict[str, Any], key: str) -> str:
    item = _required_string(value, key, 128)
    if not _safe_id(item):
        raise MtlsClientError(f"SaaS certificate response has invalid {key}")
    return item


def _safe_credential_id(value: str) -> str:
    if not 8 <= len(value) <= 128 or not all(
        character.isalnum() or character in "_-"
        for character in value
    ):
        raise MtlsClientError("Certificate credential ID is invalid")
    return value


def _required_timestamp(value: dict[str, Any], key: str) -> datetime:
    item = _required_string(value, key, 64)
    return _parse_timestamp(item)


def _optional_timestamp(value: dict[str, Any], key: str) -> datetime | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise MtlsClientError(f"SaaS certificate response has invalid {key}")
    return _parse_timestamp(item)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MtlsClientError("Certificate lifecycle timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise MtlsClientError("Certificate lifecycle timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_id(value: str) -> bool:
    return 8 <= len(value) <= 128 and all(character.isalnum() or character in "._:-" for character in value)


def _safe_environment(value: str) -> bool:
    return 2 <= len(value) <= 32 and value[0].islower() and all(
        character.islower() or character.isdigit() or character == "-"
        for character in value
    )


def _sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _valid_bearer(value: str | None) -> bool:
    if not value or not value.startswith("wvn_"):
        return False
    tail = value[4:]
    return 32 <= len(tail) <= 128 and all(character.isalnum() or character in "_-" for character in tail)


def _safe_agent_version(value: str) -> str:
    if not 8 <= len(value) <= 96 or any(ord(character) < 32 for character in value):
        raise MtlsClientError("Agent version is invalid")
    return value


def _safe_error(value: str) -> str:
    compact = " ".join(value.split())
    return compact[:240] if compact else "network operation failed"
