#!/usr/bin/env python3
"""Local, non-networked mTLS identity state for the WaveMesh Node Agent.

This module is intentionally not imported by ``node_agent.py`` yet. It prepares
and validates local key/CSR/certificate generations without changing the
current bearer-authenticated observe-only runtime.
"""

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
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


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
    ca_bundle: Path
    metadata: Path


class NodeMtlsState:
    def __init__(self, root: Path = DEFAULT_TLS_ROOT, openssl_binary: str = "openssl") -> None:
        self.root = root
        self.openssl_binary = openssl_binary
        self.pending_dir = root / "pending"
        self.generations_dir = root / "generations"
        self.active_link = root / "active"

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
            os.chmod(key_temp, 0o600)
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
            os.chmod(csr_temp, 0o600)
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
                0o600,
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
        try:
            atomic_write_bytes(certificate_temp, certificate_bytes, 0o600)
            atomic_write_bytes(ca_temp, ca_bytes, 0o600)
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
                generation_dir.mkdir(mode=0o700)
                atomic_write_bytes(generation_dir / "client.key", self.pending_key.read_bytes(), 0o600)
                atomic_write_bytes(generation_dir / "client.crt", certificate_bytes, 0o644)
                atomic_write_bytes(generation_dir / "ca.crt", ca_bytes, 0o644)
                atomic_write_json(
                    generation_dir / "metadata.json",
                    {
                        "activated_at": format_timestamp(datetime.now(timezone.utc)),
                        "generation": generation,
                        "identity_uri_hash": sha256_text(expected_identity_uri),
                        "public_key_hash": pending.public_key_hash,
                        "request_hash": pending.request_hash,
                    },
                    0o600,
                )
                self._fsync_directory(generation_dir)
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

    def active_identity(self) -> ActiveIdentity | None:
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
        return self._validate_generation(target, expected_identity_uri=None)

    def clear_pending_request(self) -> None:
        self._ensure_layout()
        for path in (self.pending_key, self.pending_csr, self.pending_metadata):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self._fsync_directory(self.pending_dir)

    def _load_and_validate_pending(self) -> PendingCertificateRequest:
        self._ensure_safe_regular_file(self.pending_key, 0o600)
        self._ensure_safe_regular_file(self.pending_csr, 0o600)
        self._ensure_safe_regular_file(self.pending_metadata, 0o600)
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
        ca = directory / "ca.crt"
        metadata_path = directory / "metadata.json"
        self._ensure_safe_regular_file(key, 0o600)
        self._ensure_safe_regular_file(certificate, 0o644, allow_stricter=True)
        self._ensure_safe_regular_file(ca, 0o644, allow_stricter=True)
        self._ensure_safe_regular_file(metadata_path, 0o600)
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

    def _ensure_safe_regular_file(self, path: Path, expected_mode: int, allow_stricter: bool = False) -> None:
        if path.is_symlink() or not path.is_file():
            raise MtlsStateError(f"mTLS state file is missing or unsafe: {path.name}")
        actual_mode = path.stat().st_mode & 0o777
        if allow_stricter:
            if actual_mode & ~expected_mode:
                raise MtlsStateError(f"mTLS state file permissions are too broad: {path.name}")
        elif actual_mode != expected_mode:
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
        os.chmod(path, 0o600)
        return path

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: dict[str, Any], mode: int) -> None:
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_bytes(path, content, mode)


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


def safe_error(value: str) -> str:
    compact = " ".join(value.split())
    return compact[:240] if compact else "operation failed"
