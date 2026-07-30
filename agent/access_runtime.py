#!/usr/bin/env python3
"""Idempotent local 3X-UI access provisioning for the WaveMesh Node Agent."""

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
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        command = json.loads(args.request.read_text(encoding="utf-8"))
        config = json.loads(args.config.read_text(encoding="utf-8"))
        result = provision(command, config, args.state_root)
        atomic_json(args.output, result)
        print("access_provision=PASS")
        return 0
    except Exception as exc:
        print(f"access_provision=FAIL code={type(exc).__name__.upper()}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
