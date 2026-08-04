#!/usr/bin/env python3
"""Local, non-networked mTLS identity state for the WaveMesh Node Agent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
from typing import Any

DEFAULT_TLS_ROOT = Path("/etc/wavemesh-agent/tls")
MAX_PEM_BYTES = 128 * 1024
SAFE_IDENTITY_PATTERN = re.compile(r"^spiffe://wavevpn/[a-z][a-z0-9-]{1,31}/tenant/[A-Za-z0-9._:%-]{1,256}/node/[A-Za-z0-9._:%-]{1,256}$")
SAFE_OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
PRIVATE_FILE_MODE = 0o600


class MtlsStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class PendingCertificateRequest:
    csr_pem: str
    request_hash: str
    public_key_hash: str
    created_at: str


@dataclass(frozen=True)
class ActiveIdentity:
    generation: str
    directory: Path
    private_key: Path
    certificate: Path
    certificate_chain: Path
    ca_bundle: Path
    metadata: Path


@dataclass(frozen=True)
class PendingAcknowledgement:
    credential_id: str
    request_hash: str
    delivery_expires_at: datetime
    recorded_at: datetime


class NodeMtlsState:
    def __init__(self, root: Path = DEFAULT_TLS_ROOT, openssl_binary: str = "openssl") -> None:
        self.root = root
        self.openssl_binary = openssl_binary
        self.pending_dir = root / "pending"
        self.generations_dir = root / "generations"
        self.active_link = root / "active"
        self.acknowledgement_path = root / "acknowledgement.pending.json"

    @property
    def pending_key(self) -> Path:
        return self.pending_dir / "client.key"

    @property
    def pending_csr(self) -> Path:
        return self.pending_dir / "client.csr"

    @property
    def pending_metadata(self) -> Path:
        return self.pending_dir / "metadata.json"

    def prepare_pending_request(self) -> PendingCertificateRequest:
        self._ensure_layout()
        existing = [path.exists() for path in (self.pending_key, self.pending_csr, self.pending_metadata)]
        if any(existing) and not all(existing):
            raise MtlsStateError("Partial pending mTLS identity exists; refusing to regenerate")
        if all(existing):
            return self._load_and_validate_pending()

        key_temp = self._temporary_path(self.pending_dir, "key")
        csr_temp = self._temporary_path(self.pending_dir, "csr")
        metadata_temp = self._temporary_path(self.pending_dir, "metadata")
        try:
            self._run(
                [
                    "genpkey",
                    "-algorithm",
                    "EC",
                    "-pkeyopt",
                    "ec_paramgen_curve:P-256",
                    "-out",
                    str(key_temp),
                ]
            )
            os.chmod(key_temp, PRIVATE_FILE_MODE)
            self._fsync_file(key_temp)
            self._run(
                [
                    "req",
                    "-new",
                    "-sha256",
                    "-key",
                    str(key_temp),
                    "-subj",
                    "/CN=WaveMesh Node Agent",
                    "-out",
                    str(csr_temp),
                ]
            )
            os.chmod(csr_temp, PRIVATE_FILE_MODE)
            self._fsync_file(csr_temp)
            request_hash = self._csr_hash(csr_temp)
            public_key_hash = self._private_key_public_hash(key_temp)
            created_at = format_timestamp(datetime.now(timezone.utc))
            atomic_write_json(
                metadata_temp,
                {
                    "created_at": created_at,
                    "key_algorithm": "ECDSA_P256",
                    "profile_version": 1,
                    "public_key_hash": public_key_hash,
                    "request_hash": request_hash,
                },
                PRIVATE_FILE_MODE,
            )
            os.replace(key_temp, self.pending_key)
            os.replace(csr_temp, self.pending_csr)
            os.replace(metadata_temp, self.pending_metadata)
            self._fsync_directory(self.pending_dir)
        finally:
            for path in (key_temp, csr_temp, metadata_temp):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

        return self._load_and_validate_pending()

    def activate_pending_certificate(
        self,
        certificate_pem: str,
        ca_bundle_pem: str,
        expected_identity_uri: str,
    ) -> ActiveIdentity:
        self._ensure_layout()
        pending = self._load_and_validate_pending()
        validate_identity_uri(expected_identity_uri)
        certificate_bytes = validate_pem(certificate_pem, "CERTIFICATE")
        ca_bytes = validate_pem_bundle(ca_bundle_pem)

        certificate_temp = self._temporary_path(self.pending_dir, "certificate")
        ca_temp = self._temporary_path(self.pending_dir, "ca")
        generation_temp: Path | None = None
        try:
            atomic_write_bytes(certificate_temp, certificate_bytes, PRIVATE_FILE_MODE)
            atomic_write_bytes(ca_temp, ca_bytes, PRIVATE_FILE_MODE)
            self._validate_certificate(
                certificate_temp,
                ca_temp,
                expected_identity_uri,
                pending.public_key_hash,
            )
            certificate_der = self._run_bytes(["x509", "-in", str(certificate_temp), "-outform", "DER"])
            generation = hashlib.sha256(certificate_der).hexdigest()[:24]
            generation_dir = self.generations_dir / generation
            if generation_dir.exists():
                if generation_dir.is_symlink() or not generation_dir.is_dir():
                    raise MtlsStateError("Existing mTLS generation path is unsafe")
                active = self._validate_generation(generation_dir, expected_identity_uri)
            else:
                generation_temp = self.generations_dir / (
                    f".{generation}.{secrets.token_hex(8)}"
                )
                generation_temp.mkdir(mode=0o700)
                atomic_write_bytes(
                    generation_temp / "client.key",
                    self.pending_key.read_bytes(),
                    PRIVATE_FILE_MODE,
                )
                atomic_write_bytes(
                    generation_temp / "client.crt",
                    certificate_bytes,
                    PRIVATE_FILE_MODE,
                )
                atomic_write_bytes(
                    generation_temp / "client-chain.crt",
                    certificate_bytes + ca_bytes,
                    PRIVATE_FILE_MODE,
                )
                atomic_write_bytes(
                    generation_temp / "ca.crt",
                    ca_bytes,
                    PRIVATE_FILE_MODE,
                )
                atomic_write_json(
                    generation_temp / "metadata.json",
                    {
                        "activated_at": format_timestamp(datetime.now(timezone.utc)),
                        "generation": generation,
                        "identity_uri_hash": sha256_text(expected_identity_uri),
                        "public_key_hash": pending.public_key_hash,
                        "request_hash": pending.request_hash,
                    },
                    PRIVATE_FILE_MODE,
                )
                self._fsync_directory(generation_temp)
                os.replace(generation_temp, generation_dir)
                generation_temp = None
                self._fsync_directory(self.generations_dir)
                active = self._validate_generation(generation_dir, expected_identity_uri)

            self._activate_generation(generation)
            for path in (self.pending_key, self.pending_csr, self.pending_metadata):
                path.unlink()
            self._fsync_directory(self.pending_dir)
            return active
        finally:
            for path in (certificate_temp, ca_temp):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            if generation_temp is not None and generation_temp.exists():
                if (
                    generation_temp.is_symlink()
                    or generation_temp.parent.resolve() != self.generations_dir.resolve()
                    or not generation_temp.name.startswith(".")
                ):
                    raise MtlsStateError("Temporary mTLS generation path is unsafe")
                shutil.rmtree(generation_temp)

    def active_identity(self, expected_identity_uri: str | None = None) -> ActiveIdentity | None:
        self._ensure_layout()
        if not self.active_link.exists() and not self.active_link.is_symlink():
            return None
        if not self.active_link.is_symlink():
            raise MtlsStateError("Active mTLS identity path is not a symlink")
        raw_target = os.readlink(self.active_link)
        target = (self.root / raw_target).resolve()
        generations_root = self.generations_dir.resolve()
        if target.parent != generations_root:
            raise MtlsStateError("Active mTLS identity points outside the generations directory")
        return self._validate_generation(target, expected_identity_uri=expected_identity_uri)

    def deactivate_active_identity(self) -> str | None:
        """Remove only the active selector while retaining immutable generations for audit."""
        self._ensure_layout()
        if not self.active_link.exists() and not self.active_link.is_symlink():
            return None
        if not self.active_link.is_symlink():
            raise MtlsStateError("Active mTLS identity path is not a symlink")
        raw_target = os.readlink(self.active_link)
        target = (self.root / raw_target).resolve()
        generations_root = self.generations_dir.resolve()
        if target.parent != generations_root or not re.fullmatch(r"[a-f0-9]{24}", target.name):
            raise MtlsStateError("Active mTLS identity points outside the generations directory")
        self.active_link.unlink()
        self._fsync_directory(self.root)
        return target.name

    def active_request_hash(self, active: ActiveIdentity | None = None) -> str | None:
        identity = active or self.active_identity()
        if identity is None:
            return None
        return require_sha256(read_json_object(identity.metadata), "request_hash")

    def record_pending_acknowledgement(
        self,
        credential_id: str,
        request_hash: str,
        delivery_expires_at: datetime,
    ) -> PendingAcknowledgement:
        self._ensure_layout()
        if not SAFE_OPAQUE_ID_PATTERN.fullmatch(credential_id):
            raise MtlsStateError("mTLS acknowledgement credential ID is invalid")
        if not SHA256_PATTERN.fullmatch(request_hash):
            raise MtlsStateError("mTLS acknowledgement request hash is invalid")
        if delivery_expires_at.tzinfo is None:
            raise MtlsStateError("mTLS delivery expiry must include a timezone")
        normalized_expiry = delivery_expires_at.astimezone(timezone.utc).replace(
            microsecond=(delivery_expires_at.microsecond // 1000) * 1000
        )

        existing = self.pending_acknowledgement()
        if existing is not None:
            if (
                existing.credential_id != credential_id
                or existing.request_hash != request_hash
                or existing.delivery_expires_at != normalized_expiry
            ):
                raise MtlsStateError("Conflicting mTLS acknowledgement is already pending")
            return existing

        recorded_at = datetime.now(timezone.utc)
        atomic_write_json(
            self.acknowledgement_path,
            {
                "credential_id": credential_id,
                "delivery_expires_at": format_timestamp(normalized_expiry),
                "recorded_at": format_timestamp(recorded_at),
                "request_hash": request_hash,
            },
            PRIVATE_FILE_MODE,
        )
        self._fsync_directory(self.root)
        return PendingAcknowledgement(
            credential_id=credential_id,
            request_hash=request_hash,
            delivery_expires_at=normalized_expiry,
            recorded_at=recorded_at,
        )

    def pending_acknowledgement(self) -> PendingAcknowledgement | None:
        self._ensure_layout()
        if not self.acknowledgement_path.exists():
            return None
        self._ensure_safe_regular_file(self.acknowledgement_path)
        value = read_json_object(self.acknowledgement_path)
        credential_id = require_text(value, "credential_id", 128)
        if not SAFE_OPAQUE_ID_PATTERN.fullmatch(credential_id):
            raise MtlsStateError("mTLS acknowledgement credential ID is invalid")
        request_hash = require_sha256(value, "request_hash")
        delivery_expires_at = parse_timestamp(
            require_text(value, "delivery_expires_at", 64),
            "delivery expiry",
        )
        recorded_at = parse_timestamp(
            require_text(value, "recorded_at", 64),
            "acknowledgement timestamp",
        )
        return PendingAcknowledgement(
            credential_id=credential_id,
            request_hash=request_hash,
            delivery_expires_at=delivery_expires_at,
            recorded_at=recorded_at,
        )

    def clear_pending_acknowledgement(self, credential_id: str) -> None:
        pending = self.pending_acknowledgement()
        if pending is None:
            return
        if pending.credential_id != credential_id:
            raise MtlsStateError("Refusing to clear another mTLS acknowledgement")
        self.acknowledgement_path.unlink()
        self._fsync_directory(self.root)

    def clear_pending_request(self) -> None:
        self._ensure_layout()
        for path in (self.pending_key, self.pending_csr, self.pending_metadata):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self._fsync_directory(self.pending_dir)

    def _load_and_validate_pending(self) -> PendingCertificateRequest:
        self._ensure_safe_regular_file(self.pending_key)
        self._ensure_safe_regular_file(self.pending_csr)
        self._ensure_safe_regular_file(self.pending_metadata)
        self._run(["pkey", "-in", str(self.pending_key), "-check", "-noout"])
        self._run(["req", "-in", str(self.pending_csr), "-verify", "-noout"])
        metadata = read_json_object(self.pending_metadata)
        request_hash = require_sha256(metadata, "request_hash")
        public_key_hash = require_sha256(metadata, "public_key_hash")
        created_at = require_text(metadata, "created_at", 64)
        if request_hash != self._csr_hash(self.pending_csr):
            raise MtlsStateError("Pending CSR hash does not match metadata")
        if public_key_hash != self._private_key_public_hash(self.pending_key):
            raise MtlsStateError("Pending private key hash does not match metadata")
        csr_pem = self.pending_csr.read_text(encoding="utf-8")
        validate_pem(csr_pem, "CERTIFICATE REQUEST")
        return PendingCertificateRequest(
            csr_pem=csr_pem,
            request_hash=request_hash,
            public_key_hash=public_key_hash,
            created_at=created_at,
        )

    def _validate_certificate(
        self,
        certificate: Path,
        ca_bundle: Path,
        expected_identity_uri: str,
        expected_public_key_hash: str,
    ) -> None:
        self._run(["verify", "-CAfile", str(ca_bundle), str(certificate)])
        self._run(["x509", "-in", str(certificate), "-checkend", "0", "-noout"])
        certificate_hash = self._certificate_public_hash(certificate)
        if certificate_hash != expected_public_key_hash:
            raise MtlsStateError("Issued certificate public key does not match pending private key")
        san_output = self._run_text(["x509", "-in", str(certificate), "-noout", "-ext", "subjectAltName"])
        uris = re.findall(r"URI:([^,\s]+)", san_output)
        if uris != [expected_identity_uri]:
            raise MtlsStateError("Issued certificate URI identity does not match exactly")
        if re.search(r"(?:DNS:|IP Address:|email:)", san_output, re.IGNORECASE):
            raise MtlsStateError("Issued Node certificate contains a forbidden SAN type")

    def _validate_generation(self, directory: Path, expected_identity_uri: str | None) -> ActiveIdentity:
        if directory.is_symlink() or not directory.is_dir() or directory.parent.resolve() != self.generations_dir.resolve():
            raise MtlsStateError("mTLS generation directory is unsafe")
        key = directory / "client.key"
        certificate = directory / "client.crt"
        certificate_chain = directory / "client-chain.crt"
        ca = directory / "ca.crt"
        metadata_path = directory / "metadata.json"
        self._ensure_safe_regular_file(key)
        self._ensure_safe_regular_file(certificate)
        self._ensure_safe_regular_file(certificate_chain)
        self._ensure_safe_regular_file(ca)
        self._ensure_safe_regular_file(metadata_path)
        metadata = read_json_object(metadata_path)
        public_key_hash = require_sha256(metadata, "public_key_hash")
        if public_key_hash != self._private_key_public_hash(key):
            raise MtlsStateError("Active mTLS generation private key does not match metadata")
        if public_key_hash != self._certificate_public_hash(certificate):
            raise MtlsStateError("Active mTLS generation certificate does not match private key")
        self._run(["verify", "-CAfile", str(ca), str(certificate)])
        if expected_identity_uri is not None:
            expected_hash = sha256_text(expected_identity_uri)
            if metadata.get("identity_uri_hash") != expected_hash:
                raise MtlsStateError("Active mTLS generation identity hash does not match")
        return ActiveIdentity(
            generation=directory.name,
            directory=directory,
            private_key=key,
            certificate=certificate,
            certificate_chain=certificate_chain,
            ca_bundle=ca,
            metadata=metadata_path,
        )

    def _activate_generation(self, generation: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{24}", generation):
            raise MtlsStateError("mTLS generation ID is invalid")
        temporary_link = self.root / f".active.{secrets.token_hex(8)}"
        try:
            os.symlink(f"generations/{generation}", temporary_link)
            os.replace(temporary_link, self.active_link)
            self._fsync_directory(self.root)
        finally:
            try:
                temporary_link.unlink()
            except FileNotFoundError:
                pass

    def _private_key_public_hash(self, key_path: Path) -> str:
        public_der = self._run_bytes(["pkey", "-in", str(key_path), "-pubout", "-outform", "DER"])
        return hashlib.sha256(public_der).hexdigest()

    def _certificate_public_hash(self, certificate_path: Path) -> str:
        public_pem = self._run_bytes(["x509", "-in", str(certificate_path), "-pubkey", "-noout"])
        public_der = self._run_bytes(["pkey", "-pubin", "-outform", "DER"], input_bytes=public_pem)
        return hashlib.sha256(public_der).hexdigest()

    def _csr_hash(self, csr_path: Path) -> str:
        csr_der = self._run_bytes(["req", "-in", str(csr_path), "-outform", "DER"])
        return hashlib.sha256(csr_der).hexdigest()

    def _ensure_layout(self) -> None:
        if self.root.is_symlink():
            raise MtlsStateError("mTLS state root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        for directory in (self.pending_dir, self.generations_dir):
            if directory.is_symlink():
                raise MtlsStateError("mTLS state directory must not be a symlink")
            directory.mkdir(exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)

    def _ensure_safe_regular_file(self, path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise MtlsStateError(f"mTLS state file is missing or unsafe: {path.name}")
        actual_mode = path.stat().st_mode & 0o777
        if os.name != "nt" and actual_mode != PRIVATE_FILE_MODE:
            raise MtlsStateError(f"mTLS state file permissions are invalid: {path.name}")

    def _run(self, arguments: list[str]) -> None:
        self._run_bytes(arguments)

    def _run_text(self, arguments: list[str]) -> str:
        return self._run_bytes(arguments).decode("utf-8", errors="strict")

    def _run_bytes(self, arguments: list[str], input_bytes: bytes | None = None) -> bytes:
        if shutil.which(self.openssl_binary) is None:
            raise MtlsStateError("OpenSSL binary is not available")
        completed = subprocess.run(
            [self.openssl_binary, *arguments],
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            message = completed.stderr.decode("utf-8", errors="replace").strip()
            raise MtlsStateError(f"OpenSSL operation failed: {safe_error(message)}")
        return completed.stdout

    def _temporary_path(self, directory: Path, label: str) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=f".{label}.", dir=directory)
        os.close(descriptor)
        path = Path(name)
        os.chmod(path, PRIVATE_FILE_MODE)
        return path

    @staticmethod
    def _fsync_file(path: Path) -> None:
        mode = "r+b" if os.name == "nt" else "rb"
        with path.open(mode) as handle:
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fsync_directory(path)


def validate_identity_uri(value: str) -> None:
    if not SAFE_IDENTITY_PATTERN.fullmatch(value):
        raise MtlsStateError("Node mTLS identity URI is invalid")


def validate_pem(value: str, label: str) -> bytes:
    if not isinstance(value, str) or "\0" in value:
        raise MtlsStateError("PEM value is invalid")
    normalized = value.strip().replace("\r\n", "\n") + "\n"
    encoded = normalized.encode("utf-8")
    if len(encoded) > MAX_PEM_BYTES:
        raise MtlsStateError("PEM value is too large")
    begin = f"-----BEGIN {label}-----\n"
    end = f"-----END {label}-----\n"
    if not normalized.startswith(begin) or not normalized.endswith(end):
        raise MtlsStateError("PEM envelope is invalid")
    return encoded


def validate_pem_bundle(value: str) -> bytes:
    encoded = validate_pem(value, "CERTIFICATE")
    text = encoded.decode("utf-8")
    if text.count("-----BEGIN CERTIFICATE-----") != text.count("-----END CERTIFICATE-----"):
        raise MtlsStateError("CA bundle PEM blocks are unbalanced")
    return encoded


def atomic_write_bytes(path: Path, content: bytes, mode: int) -> None:
    if mode != PRIVATE_FILE_MODE:
        raise MtlsStateError("mTLS state files must use mode 0600")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, PRIVATE_FILE_MODE)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: dict[str, Any], mode: int) -> None:
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_bytes(path, content, mode)


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MtlsStateError("mTLS metadata is unreadable") from exc
    if not isinstance(decoded, dict):
        raise MtlsStateError("mTLS metadata is not an object")
    return decoded


def require_sha256(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not SHA256_PATTERN.fullmatch(item):
        raise MtlsStateError(f"mTLS metadata field is invalid: {key}")
    return item


def require_text(value: dict[str, Any], key: str, maximum: int) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or len(item) > maximum:
        raise MtlsStateError(f"mTLS metadata field is invalid: {key}")
    return item


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MtlsStateError(f"mTLS {label} is invalid") from exc
    if parsed.tzinfo is None:
        raise MtlsStateError(f"mTLS {label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def safe_error(value: str) -> str:
    compact = " ".join(value.split())
    return compact[:240] if compact else "operation failed"
