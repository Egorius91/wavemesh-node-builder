"""Bounded heartbeat correlation metadata; never standalone acceptance proof.

The existing mTLS transport must supply the actual response and certificate
binding. Producer declarations still need independently admitted runtime/source
and clock evidence before a managed journal/qualifier can accept an interval.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

HEADER = 'x-wavevpn-heartbeat-observation'
MAX_HEADER_BYTES = 2048
HASH = re.compile(r'[a-f0-9]{64}\Z')
STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z')
KEYS = frozenset({'schema_version', 'kind', 'received_at', 'request_sent_at', 'node_sha256',
    'credential_sha256', 'certificate_fingerprint_sha256', 'credential_not_before',
    'credential_expires_at', 'credential_acknowledged_at', 'api_producer', 'ingress_producer'})


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def heartbeat_node_hash(tenant_id: str, node_id: str) -> str:
    return hashlib.sha256(_encoded(['wavevpn-heartbeat-node-v1', tenant_id, node_id])).hexdigest()


def heartbeat_credential_hash(tenant_id: str, node_id: str, credential_id: str) -> str:
    return hashlib.sha256(_encoded(['wavevpn-heartbeat-credential-v1', tenant_id, node_id, credential_id])).hexdigest()


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('DUPLICATE_HEARTBEAT_METADATA_KEY')
        result[key] = value
    return result


def _timestamp(value: Any) -> datetime:
    if type(value) is not str or not STAMP.fullmatch(value):
        raise ValueError('HEARTBEAT_METADATA_TIMESTAMP')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.isoformat(timespec='milliseconds').replace('+00:00', 'Z') != value:
        raise ValueError('HEARTBEAT_METADATA_TIMESTAMP')
    return parsed


def _producer(value: Any) -> bool:
    return (type(value) is dict and set(value) == {'artifact_sha256', 'instance_sha256'}
        and all(type(item) is str and HASH.fullmatch(item) for item in value.values()))


def parse_ingress_receipt(encoded: Any) -> dict[str, Any] | None:
    """Accept only the exact canonical API+ingress schema; never log input."""
    if type(encoded) is not str or not 1 <= len(encoded) <= MAX_HEADER_BYTES or not re.fullmatch(r'[A-Za-z0-9_-]+', encoded):
        return None
    try:
        raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        if base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii') != encoded:
            return None
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('HEARTBEAT_METADATA_NUMBER')))
        if type(value) is not dict or set(value) != KEYS or _encoded(value) != raw:
            return None
        if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['kind'] != 'mtls_heartbeat_commit':
            return None
        if not _producer(value['api_producer']) or not _producer(value['ingress_producer']):
            return None
        if any(type(value[key]) is not str or not HASH.fullmatch(value[key])
                for key in ('node_sha256', 'credential_sha256', 'certificate_fingerprint_sha256')):
            return None
        times = {key: _timestamp(value[key]) for key in ('received_at', 'request_sent_at',
            'credential_not_before', 'credential_expires_at', 'credential_acknowledged_at')}
        received = times['received_at']
        if not (times['credential_not_before'] <= received and times['credential_acknowledged_at'] <= received
                and times['credential_expires_at'] > received):
            return None
        return value
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        return None


def bind_ingress_receipt(encoded: Any, *, tenant_id: str, node_id: str, request_sent_at: str,
                         certificate_fingerprint_sha256: str, now: datetime) -> dict[str, Any] | None:
    """Correlate with the real request/cert; caller still owes trusted-clock proof."""
    value = parse_ingress_receipt(encoded)
    if value is None:
        return None
    try:
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            return None
        # Agent timestamps may have microseconds; the server's Date contract
        # canonicalizes the same instant to milliseconds, without changing bytes.
        if type(request_sent_at) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', request_sent_at):
            return None
        sent = datetime.fromisoformat(request_sent_at.replace('Z', '+00:00'))
        canonical_sent = sent.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        age = (now - _timestamp(value['received_at'])).total_seconds()
        if (value['node_sha256'] != heartbeat_node_hash(tenant_id, node_id)
                or value['certificate_fingerprint_sha256'] != certificate_fingerprint_sha256
                or value['request_sent_at'] != canonical_sent or not -5 <= age <= 120
                or _timestamp(value['credential_expires_at']) <= now):
            return None
        return value
    except (ValueError, TypeError, OverflowError, AttributeError):
        return None
