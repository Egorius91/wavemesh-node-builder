#!/usr/bin/env python3
"""Read-only, certificate-validating probe of one configured public Entry IP:443.

This proves a TLS handshake only. It never qualifies Nginx's loaded config,
publishes a route, downloads a subscription or accepts a VPN/service/Node.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import time
import uuid

LIMIT = 1 << 20
TIMEOUT = 10
DOMAIN = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class ProbeUnavailable(ValueError):
    """Only a fixed, nonsecret reason may leave the probe."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProbeUnavailable("CONFIGURATION_UNAVAILABLE")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError()


def public_target(config):
    try:
        server = config["server"]
        domain, address = server["domain"], server["public_ip"]
        if not isinstance(domain, str) or not DOMAIN.fullmatch(domain.lower()):
            raise ValueError()
        if not isinstance(address, str) or "%" in address:
            raise ValueError()
        ip = ipaddress.ip_address(address)
        if (not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified
                or getattr(ip, "ipv4_mapped", None) is not None):
            raise ValueError()
        # No DNS resolution or discovery: the address is an admitted literal.
        return domain.lower(), str(ip), ip.version
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProbeUnavailable("PUBLIC_TARGET_UNAVAILABLE") from None


def _configuration(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > LIMIT
                    or (os.name == "posix" and (before.st_uid != 0 or stat.S_IMODE(before.st_mode) != 0o600))):
                raise ProbeUnavailable("CONFIGURATION_UNAVAILABLE")
            data = stream.read(LIMIT + 1)
            after = os.fstat(stream.fileno())
            current = os.stat(path, follow_symlinks=False)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if len(data) > LIMIT or identity(before) != identity(after) or identity(before) != identity(current):
            raise ProbeUnavailable("CONFIGURATION_CHANGED")
        config = json.loads(data, object_pairs_hook=_object, parse_constant=_invalid_constant)
        return config, hashlib.sha256(data).hexdigest()
    except ProbeUnavailable:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise ProbeUnavailable("CONFIGURATION_UNAVAILABLE") from None


def collect(config_path):
    started = datetime.now(timezone.utc)
    clock = time.monotonic()
    probe_id = str(uuid.uuid4())
    base = {"schema_version": 1, "scope": "public_entry_tls_only", "probe_id": probe_id,
            "started_at": started.isoformat(), "status": "NOT_VERIFIED"}
    try:
        config, digest = _configuration(Path(config_path))
        domain, address, family = public_target(config)
        context = ssl.create_default_context()
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise ProbeUnavailable("TLS_VERIFICATION_REQUIRED")
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        with socket.socket(socket.AF_INET if family == 4 else socket.AF_INET6, socket.SOCK_STREAM) as raw:
            raw.settimeout(TIMEOUT)
            raw.connect((address, 443))
            if ipaddress.ip_address(raw.getpeername()[0]) != ipaddress.ip_address(address):
                raise ProbeUnavailable("PEER_ADDRESS_MISMATCH")
            remaining = TIMEOUT - (time.monotonic() - clock)
            if remaining <= 0:
                raise ProbeUnavailable("TLS_PROBE_TIMEOUT")
            raw.settimeout(remaining)
            with context.wrap_socket(raw, server_hostname=domain) as secured:
                certificate = secured.getpeercert(binary_form=True)
                expiry = secured.getpeercert().get("notAfter")
                if not certificate or not expiry:
                    raise ProbeUnavailable("TLS_CERTIFICATE_UNAVAILABLE")
                valid_until = datetime.fromtimestamp(ssl.cert_time_to_seconds(expiry), timezone.utc)
                negotiated = secured.version()
                roots = [hashlib.sha256(item).digest() for item in context.get_ca_certs(binary_form=True)]
        _, current = _configuration(Path(config_path))
        if current != digest:
            raise ProbeUnavailable("CONFIGURATION_CHANGED")
        if valid_until <= datetime.now(timezone.utc):
            raise ProbeUnavailable("TLS_CERTIFICATE_EXPIRED")
        base.update(status="VERIFIED_TLS_ONLY", config_sha256=digest, domain=domain, address=address,
                    address_family=family, port=443, tls_version=negotiated,
                    certificate_sha256=hashlib.sha256(certificate).hexdigest(),
                    certificate_valid_until=valid_until.isoformat(),
                    loaded_trust_roots_sha256=hashlib.sha256(b''.join(sorted(roots))).hexdigest(),
                    loaded_trust_root_count=len(roots), reason=None)
    except ProbeUnavailable as error:
        base["reason"] = str(error)
    except ssl.SSLCertVerificationError:
        base["reason"] = "TLS_CERTIFICATE_VERIFICATION_FAILED"
    except (socket.timeout, TimeoutError):
        base["reason"] = "TLS_PROBE_TIMEOUT"
    except (OSError, ssl.SSLError, ValueError, OverflowError):
        base["reason"] = "TLS_HANDSHAKE_UNAVAILABLE"
    base.update(completed_at=datetime.now(timezone.utc).isoformat(), duration_ms=int((time.monotonic() - clock) * 1000))
    return base


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    report = collect(args.config)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "VERIFIED_TLS_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
