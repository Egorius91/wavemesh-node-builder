#!/usr/bin/env python3
"""Idempotent local 3X-UI access lifecycle for the WaveMesh Node Agent."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Any
from urllib import error, parse, request
import uuid

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
DAY_MILLISECONDS = 24 * 60 * 60 * 1000


class ProvisionError(RuntimeError):
    pass


class PanelClient:
    def __init__(self, config: dict[str, Any], timeout: int = 20) -> None:
        panel = object_value(config.get("panel"))
        token = str(object_value(panel.get("api_auth")).get("token") or "")
        port = integer(panel.get("listen_port"), 1, 65535)
        path = "/" + str(panel.get("path") or "").strip("/") + "/"
        if not token or not SAFE_ID.fullmatch(token):
            raise ProvisionError("3X-UI bearer API token is unavailable")
        self.base = f"http://127.0.0.1:{port}{path.rstrip('/')}"
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        req = request.Request(
            self.base + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "wavemesh-access-runtime/1",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except (error.HTTPError, error.URLError) as exc:
            raise ProvisionError("3X-UI request failed") from exc
        try:
            value = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProvisionError("3X-UI returned invalid JSON") from exc
        if not isinstance(value, dict) or value.get("success") is not True:
            raise ProvisionError("3X-UI operation was rejected")
        return value


def provision(request_value: dict[str, Any], config: dict[str, Any], state_root: Path) -> dict[str, Any]:
    access_id = safe_id(request_value.get("access_id"), "access_id")
    desired_version = integer(request_value.get("desired_version"), 1, 2_147_483_647)
    expires_at = parse_time(request_value.get("expires_at"))
    device_limit = integer(request_value.get("device_limit"), 0, 10_000)
    quota_bytes = integer(request_value.get("quota_bytes"), 0, 9_223_372_036_854_775_807)
    state_path = state_root / f"{access_id}.{desired_version}.json"
    state = load_private_state(state_path)
    if state is None:
        state = {
            "access_id": access_id,
            "desired_version": desired_version,
            "panel_email": f"wm_{access_id[:48]}_{desired_version}",
            "client_uuid": str(uuid.uuid4()),
            "sub_id": secrets.token_urlsafe(24),
        }
        atomic_json(state_path, state)
    validate_state(state, access_id, desired_version)

    subscription = object_value(object_value(config.get("network")).get("subscription"))
    if subscription.get("backend") != "xui-native":
        raise ProvisionError("xui-native subscription backend is required")
    domain = str(object_value(config.get("server")).get("domain") or "")
    sub_path = "/" + str(subscription.get("path") or "").strip("/") + "/"
    if not domain or ".." in sub_path or len(sub_path) < 12:
        raise ProvisionError("Native subscription settings are invalid")

    panel = PanelClient(config)
    inbound_ids = visible_vless_inbound_ids(panel.call("GET", "/panel/api/inbounds/list"))
    if not inbound_ids:
        raise ProvisionError("No public VLESS inbound is available")
    email = str(state["panel_email"])
    existing = get_client(panel, email)
    if existing is None:
        panel.call(
            "POST",
            "/panel/api/clients/add",
            {
                "client": {
                    "email": email,
                    "limitIp": device_limit,
                    "totalGB": quota_bytes,
                    "expiryTime": int(expires_at.timestamp() * 1000),
                    "enable": True,
                    "tgId": 0,
                    "subId": state["sub_id"],
                    "reset": 0,
                    "id": state["client_uuid"],
                    "flow": "",
                },
                "inboundIds": inbound_ids,
            },
        )
        existing = get_client(panel, email)
    assert_matching_client(existing, state, inbound_ids)
    links = panel.call("GET", f"/panel/api/clients/subLinks/{parse.quote(str(state['sub_id']), safe='')}")
    if not subscription_links_ready(links):
        raise ProvisionError("Native subscription links are not ready")

    return {
        "desired_version": desired_version,
        "panel_email": email,
        "client_uuid": state["client_uuid"],
        "sub_id": state["sub_id"],
        "primary_inbound_id": inbound_ids[0],
        "protocol": "vless",
        "subscription_url": f"https://{domain}{sub_path}{state['sub_id']}",
    }


def update_entitlements(
    request_value: dict[str, Any],
    config: dict[str, Any],
    state_root: Path,
) -> dict[str, Any]:
    """Update expiry and limits while preserving the accepted access identity."""
    access_id = safe_id(request_value.get("access_id"), "access_id")
    desired_version = integer(request_value.get("desired_version"), 2, 2_147_483_647)
    expires_at = parse_time(request_value.get("expires_at"))
    device_limit = integer(request_value.get("device_limit"), 0, 10_000)
    quota_bytes = integer(request_value.get("quota_bytes"), 0, 9_223_372_036_854_775_807)
    expires_at_ms = int(expires_at.timestamp() * 1000)
    if request_value.get("enabled") is not True:
        raise ProvisionError("Disabled entitlement updates are unsupported")

    state_path = state_root / f"{access_id}.{desired_version}.json"
    state = load_private_state(state_path)
    if state is None:
        previous = latest_previous_state(state_root, access_id, desired_version)
        state = {
            "access_id": access_id,
            "desired_version": desired_version,
            "panel_email": previous["panel_email"],
            "client_uuid": previous["client_uuid"],
            "sub_id": previous["sub_id"],
        }
        atomic_json(state_path, state)
    validate_state(state, access_id, desired_version)

    subscription = object_value(object_value(config.get("network")).get("subscription"))
    if subscription.get("backend") != "xui-native":
        raise ProvisionError("xui-native subscription backend is required")
    domain = str(object_value(config.get("server")).get("domain") or "")
    sub_path = "/" + str(subscription.get("path") or "").strip("/") + "/"
    if not domain or ".." in sub_path or len(sub_path) < 12:
        raise ProvisionError("Native subscription settings are invalid")

    panel = PanelClient(config)
    inbound_ids = visible_vless_inbound_ids(panel.call("GET", "/panel/api/inbounds/list"))
    if not inbound_ids:
        raise ProvisionError("No public VLESS inbound is available")
    email = str(state["panel_email"])
    existing = get_client(panel, email)
    assert_matching_client(existing, state, inbound_ids)

    verified = existing
    if not entitlements_match(
        existing,
        expires_at_ms=expires_at_ms,
        device_limit=device_limit,
        quota_bytes=quota_bytes,
    ):
        raw_client = existing.get("client") if isinstance(existing, dict) else None
        client = dict(raw_client if isinstance(raw_client, dict) else existing or {})
        client.pop("uuid", None)
        client.pop("inboundIds", None)
        client.update(
            {
                "email": email,
                "limitIp": device_limit,
                "totalGB": quota_bytes,
                "expiryTime": expires_at_ms,
                "enable": True,
                "tgId": integer(client.get("tgId") or 0, 0, 9_223_372_036_854_775_807),
                "subId": state["sub_id"],
                "reset": integer(client.get("reset") or 0, 0, 2_147_483_647),
                "id": state["client_uuid"],
                "flow": str(client.get("flow") or ""),
            }
        )
        update_error: ProvisionError | None = None
        try:
            panel.call(
                "POST",
                f"/panel/api/clients/update/{parse.quote(email, safe='')}",
                client,
            )
        except ProvisionError as exc:
            update_error = exc

        verified = get_client(panel, email)
        assert_matching_client(verified, state, inbound_ids)
        if not entitlements_match(
            verified,
            expires_at_ms=expires_at_ms,
            device_limit=device_limit,
            quota_bytes=quota_bytes,
        ):
            adjustment = safe_bulk_entitlement_adjustment(
                verified,
                expires_at_ms=expires_at_ms,
                device_limit=device_limit,
                quota_bytes=quota_bytes,
            )
            if adjustment is not None:
                add_days, add_bytes = adjustment
                if add_days != 0 or add_bytes != 0:
                    panel.call(
                        "POST",
                        "/panel/api/clients/bulkAdjust",
                        {
                            "emails": [email],
                            "addDays": add_days,
                            "addBytes": add_bytes,
                        },
                    )
                    verified = get_client(panel, email)
                    assert_matching_client(verified, state, inbound_ids)

        try:
            assert_entitlements(
                verified,
                expires_at_ms=expires_at_ms,
                device_limit=device_limit,
                quota_bytes=quota_bytes,
            )
        except ProvisionError:
            if update_error is not None:
                raise update_error
            raise

    links = panel.call(
        "GET",
        f"/panel/api/clients/subLinks/{parse.quote(str(state['sub_id']), safe='')}",
    )
    if not subscription_links_ready(links):
        raise ProvisionError("Native subscription links are not ready")

    return {
        "desired_version": desired_version,
        "panel_email": email,
        "client_uuid": state["client_uuid"],
        "sub_id": state["sub_id"],
        "primary_inbound_id": inbound_ids[0],
        "protocol": "vless",
        "subscription_url": f"https://{domain}{sub_path}{state['sub_id']}",
    }


def latest_previous_state(
    state_root: Path,
    access_id: str,
    desired_version: int,
) -> dict[str, Any]:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for state_path in state_root.glob(f"{access_id}.*.json"):
        state = load_private_state(state_path)
        if state is None or state.get("access_id") != access_id:
            continue
        version = integer(state.get("desired_version"), 1, 2_147_483_647)
        if version >= desired_version:
            continue
        validate_state(state, access_id, version)
        candidates.append((version, state))
    if not candidates:
        raise ProvisionError("Previous durable access identity is missing")
    return max(candidates, key=lambda item: item[0])[1]


def assert_entitlements(
    record: dict[str, Any] | None,
    *,
    expires_at_ms: int,
    device_limit: int,
    quota_bytes: int,
) -> None:
    if not record:
        raise ProvisionError("3X-UI entitlement verification failed")
    client = record.get("client") if isinstance(record.get("client"), dict) else record
    if (
        client.get("enable") is not True
        or integer(client.get("expiryTime"), 0, 9_223_372_036_854_775_807) != expires_at_ms
        or integer(client.get("limitIp"), 0, 10_000) != device_limit
        or integer(client.get("totalGB"), 0, 9_223_372_036_854_775_807) != quota_bytes
    ):
        raise ProvisionError("3X-UI entitlement update was not verified")


def entitlements_match(
    record: dict[str, Any] | None,
    *,
    expires_at_ms: int,
    device_limit: int,
    quota_bytes: int,
) -> bool:
    try:
        assert_entitlements(
            record,
            expires_at_ms=expires_at_ms,
            device_limit=device_limit,
            quota_bytes=quota_bytes,
        )
    except ProvisionError:
        return False
    return True


def safe_bulk_entitlement_adjustment(
    record: dict[str, Any] | None,
    *,
    expires_at_ms: int,
    device_limit: int,
    quota_bytes: int,
) -> tuple[int, int] | None:
    """Return an additive 3X-UI repair only when its semantics are unambiguous."""
    if not record:
        return None
    client = record.get("client") if isinstance(record.get("client"), dict) else record
    if client.get("enable") is not True:
        return None
    if integer(client.get("limitIp"), 0, 10_000) != device_limit:
        return None

    current_expiry_ms = integer(client.get("expiryTime"), 0, 9_223_372_036_854_775_807)
    expiry_delta_ms = expires_at_ms - current_expiry_ms
    if expiry_delta_ms % DAY_MILLISECONDS != 0:
        return None
    add_days = expiry_delta_ms // DAY_MILLISECONDS
    if not -3650 <= add_days <= 3650:
        return None

    current_quota_bytes = integer(client.get("totalGB"), 0, 9_223_372_036_854_775_807)
    if current_quota_bytes == quota_bytes:
        add_bytes = 0
    elif quota_bytes == 0 and current_quota_bytes > 0:
        add_bytes = -current_quota_bytes
    else:
        return None
    return add_days, add_bytes


def cleanup_previous(request_value: dict[str, Any], config: dict[str, Any], state_root: Path) -> int:
    """Remove only older durable identities after SaaS accepted the replacement."""
    access_id = safe_id(request_value.get("access_id"), "access_id")
    desired_version = integer(request_value.get("desired_version"), 2, 2_147_483_647)
    current_path = state_root / f"{access_id}.{desired_version}.json"
    current = load_private_state(current_path)
    if current is None:
        raise ProvisionError("Replacement durable state is missing")
    validate_state(current, access_id, desired_version)

    panel = PanelClient(config)
    removed = 0
    for state_path in sorted(state_root.glob(f"{access_id}.*.json")):
        if state_path == current_path:
            continue
        state = load_private_state(state_path)
        if state is None or state.get("access_id") != access_id:
            continue
        version = integer(state.get("desired_version"), 1, 2_147_483_647)
        if version >= desired_version:
            continue
        email = str(state.get("panel_email") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", email):
            raise ProvisionError("Previous durable panel identity is invalid")
        if get_client(panel, email) is not None:
            panel.call(
                "POST",
                f"/panel/api/clients/del/{parse.quote(email, safe='')}",
            )
            if get_client(panel, email) is not None:
                raise ProvisionError("Previous 3X-UI client cleanup was not verified")
            removed += 1
    return removed


def visible_vless_inbound_ids(response: dict[str, Any]) -> list[int]:
    result = []
    for item in response.get("obj") or []:
        if not isinstance(item, dict) or not item.get("enable", True):
            continue
        if str(item.get("remark") or "").startswith("--!") or item.get("protocol") != "vless":
            continue
        inbound_id = item.get("id")
        if isinstance(inbound_id, int) and inbound_id > 0:
            result.append(inbound_id)
    return sorted(set(result))


def get_client(panel: PanelClient, email: str) -> dict[str, Any] | None:
    try:
        value = panel.call("GET", f"/panel/api/clients/get/{parse.quote(email, safe='')}")
    except ProvisionError:
        return None
    obj = value.get("obj")
    return obj if isinstance(obj, dict) else None


def assert_matching_client(record: dict[str, Any] | None, state: dict[str, Any], inbound_ids: list[int]) -> None:
    if not record:
        raise ProvisionError("3X-UI client verification failed")
    client = record.get("client") if isinstance(record.get("client"), dict) else record
    attached = record.get("inboundIds") or client.get("inboundIds") or []
    client_uuid = client.get("uuid")
    if not client_uuid and isinstance(client.get("id"), str):
        client_uuid = client["id"]
    if (
        client.get("email") != state["panel_email"]
        or client_uuid != state["client_uuid"]
        or client.get("subId") != state["sub_id"]
        or not set(inbound_ids).issubset({int(value) for value in attached if str(value).isdigit()})
    ):
        raise ProvisionError("Existing 3X-UI client conflicts with durable access state")


def subscription_links_ready(response: dict[str, Any]) -> bool:
    value = response.get("obj")
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, list) and any(str(item).strip() for item in value)


def load_private_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ProvisionError("Access state path is unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProvisionError("Access state is invalid")
    return value


def validate_state(state: dict[str, Any], access_id: str, desired_version: int) -> None:
    if state.get("access_id") != access_id or state.get("desired_version") != desired_version:
        raise ProvisionError("Durable access state does not match the command")
    uuid.UUID(str(state.get("client_uuid")), version=4)
    safe_id(state.get("sub_id"), "sub_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", str(state.get("panel_email") or "")):
        raise ProvisionError("Durable panel identity is invalid")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def object_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def integer(value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ProvisionError("Integer value is invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ProvisionError("Integer value is invalid") from exc
    if result < minimum or result > maximum:
        raise ProvisionError("Integer value is outside allowed bounds")
    return result


def safe_id(value: Any, name: str) -> str:
    result = str(value or "")
    if not SAFE_ID.fullmatch(result):
        raise ProvisionError(f"{name} is invalid")
    return result


def parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProvisionError("expires_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ProvisionError("expires_at must include a timezone")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=Path("/etc/wavemesh-node/config.json"))
    parser.add_argument("--state-root", type=Path, default=Path("/var/lib/wavemesh-agent/access"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cleanup-previous", action="store_true")
    args = parser.parse_args()
    try:
        command = json.loads(args.request.read_text(encoding="utf-8"))
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if args.cleanup_previous:
            removed = cleanup_previous(command, config, args.state_root)
            print(f"access_cleanup=PASS removed={removed}")
            return 0
        if args.output is None:
            raise ProvisionError("output is required for access lifecycle execution")
        operation = str(command.get("operation") or "access.provision")
        if operation == "access.update_entitlements":
            result = update_entitlements(command, config, args.state_root)
        elif operation in {"access.provision", "access.replace_credential"}:
            result = provision(command, config, args.state_root)
        else:
            raise ProvisionError("Unsupported access lifecycle operation")
        atomic_json(args.output, result)
        print(f"access_runtime=PASS operation={operation}")
        return 0
    except Exception as exc:
        print(f"access_runtime=FAIL code={type(exc).__name__.upper()}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
