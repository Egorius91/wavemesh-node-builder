"""Read-only forensic expiry admission. Never used by the mTLS transport."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat

from node_mtls_state import NodeMtlsState, MtlsStateError, read_json_object, require_sha256, sha256_text


def private_path(path: Path, directory: bool = False, max_bytes: int | None = 256 * 1024) -> None:
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or (not directory and info.st_nlink != 1):
        raise MtlsStateError("Expiry admission path is unsafe")
    if not directory and max_bytes is not None and info.st_size > max_bytes:
        raise MtlsStateError("Expiry admission file too large")
    if os.name != "nt" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)):
        raise MtlsStateError("Expiry admission ownership or permissions are unsafe")


def certificate_dates(state: NodeMtlsState, pem: bytes) -> tuple[datetime, datetime]:
    text = state._run_bytes(["x509", "-noout", "-startdate", "-enddate"], input_bytes=pem).decode("ascii")
    dates = [datetime.strptime(line.split("=", 1)[1], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
             for line in text.strip().splitlines()]
    if len(dates) != 2:
        raise MtlsStateError("Expiry admission certificate dates invalid")
    return dates[0], dates[1]


def inspect_expired(state: NodeMtlsState, identity: str, baseline: dict, minimum_ca_horizon_seconds: int = 0) -> dict:
    """Require exact acknowledged baseline; suppress time only for chain signatures.

    Current time is explicitly checked for every CA and leaf notBefore. No state
    creation/chmod, selector activation, secret output, or network calls occur.
    """
    if baseline.get("acknowledged") is not True or baseline.get("revoked") is not False:
        raise MtlsStateError("Expiry baseline is not acknowledged and non-revoked")
    if baseline.get("quarantined") is not False or baseline.get("compromised") is not False:
        raise MtlsStateError("Expiry baseline is quarantined or compromised")
    for path in (state.root, state.generations_dir, state.pending_dir):
        private_path(path, True)
    if not state.active_link.is_symlink():
        raise MtlsStateError("Expiry admission requires active generation selector")
    raw = os.readlink(state.active_link)
    if not re.fullmatch(r"generations/[a-f0-9]{24}", raw):
        raise MtlsStateError("Expiry admission selector is unsafe")
    generation = state.root / raw
    private_path(generation, True)
    files = {name: generation / name for name in ("client.key", "client.crt", "client-chain.crt", "ca.crt", "metadata.json")}
    for path in files.values():
        private_path(path)
    metadata = read_json_object(files["metadata.json"])
    cert = files["client.crt"].read_bytes()
    ca = files["ca.crt"].read_bytes()
    if len(cert) > 128 * 1024 or len(ca) > 128 * 1024:
        raise MtlsStateError("Expiry admission PEM size invalid")
    der = state._run_bytes(["x509", "-in", str(files["client.crt"]), "-outform", "DER"])
    fingerprint = hashlib.sha256(der).hexdigest()
    if generation.name != fingerprint[:24] or metadata.get("generation") != generation.name:
        raise MtlsStateError("Expiry generation fingerprint mismatch")
    if metadata.get("identity_uri_hash") != sha256_text(identity):
        raise MtlsStateError("Expiry identity metadata mismatch")
    public_hash = require_sha256(metadata, "public_key_hash")
    state._run(["pkey", "-in", str(files["client.key"]), "-check", "-noout"])
    if public_hash != state._private_key_public_hash(files["client.key"]) or public_hash != state._certificate_public_hash(files["client.crt"]):
        raise MtlsStateError("Expiry key certificate metadata mismatch")
    san = state._run_text(["x509", "-in", str(files["client.crt"]), "-noout", "-ext", "subjectAltName"])
    if len(san.splitlines()) < 2 or " ".join(line.strip() for line in san.splitlines()[1:]) != "URI:" + identity:
        raise MtlsStateError("Expiry certificate SAN mismatch")
    if files["client-chain.crt"].read_bytes() != cert + ca:
        raise MtlsStateError("Expiry chain metadata mismatch")
    serial = state._run_text(["x509", "-in", str(files["client.crt"]), "-noout", "-serial"]).strip().split("=", 1)[1].lower()
    if sha256_text(serial) != baseline.get("certificateSerialHash") or fingerprint != baseline.get("certificateFingerprintHash"):
        raise MtlsStateError("Expiry acknowledged credential mismatch")
    ca_hash = hashlib.sha256(ca).hexdigest()
    if ca_hash != baseline.get("trustedCaBundleSha256") or public_hash != baseline.get("publicKeyHash"):
        raise MtlsStateError("Expiry trust baseline mismatch")
    now = datetime.now(timezone.utc)
    not_before, expiry = certificate_dates(state, cert)
    if not not_before < expiry <= now:
        raise MtlsStateError("Leaf certificate is not expired")
    expected_expiry = datetime.fromisoformat(str(baseline.get("certificateExpiresAt", "")).replace("Z", "+00:00"))
    if expiry != expected_expiry:
        raise MtlsStateError("Expiry acknowledged timestamp mismatch")
    ca_blocks = re.findall(rb"-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----", ca, re.S)
    if not ca_blocks or re.sub(rb"-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----", b"", ca, flags=re.S).strip():
        raise MtlsStateError("Expiry CA bundle invalid")
    ca_ends = []
    for block in ca_blocks:
        start, end = certificate_dates(state, block)
        if not start <= now < end:
            raise MtlsStateError("Expiry CA is not currently valid")
        ca_ends.append(end)
    ca_valid_until = min(ca_ends)
    if (ca_valid_until - now).total_seconds() <= minimum_ca_horizon_seconds:
        raise MtlsStateError("Expiry CA cannot cover the accepted leaf horizon")
    state._run(["verify", "-no_check_time", "-purpose", "sslclient", "-CAfile", str(files["ca.crt"]), str(files["client.crt"])])
    if state.acknowledgement_path.exists() or state.acknowledgement_path.is_symlink():
        raise MtlsStateError("Unrelated acknowledgement pending")
    paths = (state.pending_key, state.pending_csr, state.pending_metadata)
    present = [path.exists() or path.is_symlink() for path in paths]
    if any(present) and not all(present):
        raise MtlsStateError("Partial pending identity requires reconciliation")
    pending_hash = None
    pending_public_hash = None
    pem_hash = None
    if all(present):
        for path in paths:
            private_path(path)
        pending = state._load_and_validate_pending()
        pending_hash = pending.request_hash
        pending_public_hash = pending.public_key_hash
        pem_hash = hashlib.sha256((pending.csr_pem.strip().replace("\r\n", "\n") + "\n").encode()).hexdigest()
    return {"ok": True, "code": "INTACT_EXPIRED", "certificateFingerprintHash": fingerprint,
            "certificateSerialHash": sha256_text(serial), "publicKeyHash": public_hash,
            "identityUriHash": sha256_text(identity), "trustedCaBundleSha256": ca_hash,
            "certificateExpiresAt": expiry.isoformat(), "localRequestHash": pending_hash,
            "pendingPublicKeyHash": pending_public_hash, "pemRequestHash": pem_hash,
            "trustedCaValidUntil": ca_valid_until.isoformat()}
