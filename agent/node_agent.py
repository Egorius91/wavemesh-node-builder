#!/usr/bin/env python3
"""WaveMesh observe-only Node Agent.

The agent sends redacted heartbeat and topology health observations to WaveVPN
SaaS. It never polls or executes mutation commands. Replacement bearer tokens
are generated locally; SaaS receives only their SHA-256 hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib import error, request

AGENT_VERSION = "0.2.0-observe-only"
DEFAULT_ENV_PATH = Path("/etc/wavemesh-agent/agent.env")
DEFAULT_RUNTIME_PATH = Path("/etc/wavemesh-agent/runtime.json")
NODE_CONFIG_PATH = Path("/etc/wavemesh-node/config.json")
TOKEN_PREFIX = "wvn_"
FORBIDDEN_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "manifest",
    "password",
    "privatekey",
    "secret",
    "subid",
    "subscriptionuri",
    "subscriptionurl",
    "token",
    "uuid",
)

LOG = logging.getLogger("wavemesh-node-agent")
_STOP = False


class AgentError(RuntimeError):
    pass


class ApiError(AgentError):
    def __init__(self, status: int, code: str, retryable: bool, message: str = "") -> None:
        super().__init__(f"HTTP {status} {code}: {message}".strip())
        self.status = status
        self.code = code
        self.retryable = retryable


@dataclass
class AgentConfig:
    env_path: Path
    api_base: str
    node_id: str
    tenant_id: str
    agent_token: str
    token_expires_at: datetime
    mode: str
    observed_version: int
    heartbeat_seconds: int
    observation_seconds: int
    rotate_before_seconds: int
    request_timeout_seconds: int
    runtime_path: Path

    @classmethod
    def load(cls, env_path: Path) -> "AgentConfig":
        values = read_env_file(env_path)
        required = (
            "WAVEMESH_API_BASE",
            "WAVEMESH_NODE_ID",
            "WAVEMESH_TENANT_ID",
            "WAVEMESH_AGENT_TOKEN",
            "WAVEMESH_AGENT_TOKEN_EXPIRES_AT",
        )
        missing = [key for key in required if not values.get(key)]
        if missing:
            raise AgentError("Missing agent settings: " + ", ".join(missing))

        mode = values.get("WAVEMESH_AGENT_MODE", "observe-only")
        if mode != "observe-only":
            raise AgentError("Only WAVEMESH_AGENT_MODE=observe-only is supported")

        token = values["WAVEMESH_AGENT_TOKEN"]
        if not valid_token(token):
            raise AgentError("WAVEMESH_AGENT_TOKEN has an invalid format")

        return cls(
            env_path=env_path,
            api_base=values["WAVEMESH_API_BASE"].rstrip("/"),
            node_id=values["WAVEMESH_NODE_ID"],
            tenant_id=values["WAVEMESH_TENANT_ID"],
            agent_token=token,
            token_expires_at=parse_timestamp(values["WAVEMESH_AGENT_TOKEN_EXPIRES_AT"]),
            mode=mode,
            observed_version=bounded_int(values.get("WAVEMESH_OBSERVED_VERSION"), 0, 0, 2_147_483_647),
            heartbeat_seconds=bounded_int(values.get("WAVEMESH_AGENT_HEARTBEAT_SECONDS"), 60, 15, 3600),
            observation_seconds=bounded_int(values.get("WAVEMESH_AGENT_OBSERVATION_SECONDS"), 300, 60, 86400),
            rotate_before_seconds=bounded_int(values.get("WAVEMESH_AGENT_ROTATE_BEFORE_SECONDS"), 21600, 3600, 604800),
            request_timeout_seconds=bounded_int(values.get("WAVEMESH_AGENT_REQUEST_TIMEOUT_SECONDS"), 30, 5, 120),
            runtime_path=Path(values.get("WAVEMESH_AGENT_RUNTIME_PATH", str(DEFAULT_RUNTIME_PATH))),
        )

    @property
    def pending_rotation_path(self) -> Path:
        return self.env_path.with_name("rotation.pending")

    def save_rotated_token(self, token: str, expires_at: datetime) -> None:
        values = read_env_file(self.env_path)
        values["WAVEMESH_AGENT_TOKEN"] = token
        values["WAVEMESH_AGENT_TOKEN_EXPIRES_AT"] = format_timestamp(expires_at)
        write_env_file(self.env_path, values)
        self.agent_token = token
        self.token_expires_at = expires_at


class NodeAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.last_health_state: dict[str, Any] = {
            "mode": "observe_only",
            "node_status": "unknown",
            "routes": [],
            "auto_routes": [],
        }
        self.runtime = read_json_file(config.runtime_path, default={})

    def run(self, once: bool = False) -> None:
        next_observation = 0.0
        while not _STOP:
            started = time.monotonic()
            try:
                self.rotate_if_due()
                if time.monotonic() >= next_observation:
                    self.collect_and_send_observation()
                    next_observation = time.monotonic() + self.config.observation_seconds
                self.send_heartbeat()
            except ApiError as exc:
                LOG.warning("SaaS request failed: status=%s code=%s retryable=%s", exc.status, exc.code, exc.retryable)
            except Exception as exc:  # noqa: BLE001 - long-running service boundary
                LOG.exception("Agent cycle failed: %s", exc)

            if once:
                return
            elapsed = time.monotonic() - started
            stop_aware_sleep(max(1.0, self.config.heartbeat_seconds - elapsed))

    def rotate_if_due(self) -> None:
        now = datetime.now(timezone.utc)
        if now + timedelta(seconds=self.config.rotate_before_seconds) < self.config.token_expires_at:
            return

        replacement = self.load_or_create_pending_replacement()
        try:
            response = self.api_json(
                "POST",
                f"internal/v1/nodes/{self.config.node_id}/credentials/rotate",
                {
                    "next_token_hash": token_hash(replacement),
                    "agent_version": AGENT_VERSION,
                },
                expected=(201,),
            )
        except ApiError as exc:
            if exc.code in {
                "NODE_CREDENTIAL_HASH_CONFLICT",
                "NODE_CREDENTIAL_REUSE_FORBIDDEN",
                "NODE_CREDENTIAL_NOT_ACTIVE",
            }:
                self.clear_pending_replacement()
            raise

        expires_at = parse_timestamp(require_string(response, "expires_at"))
        self.config.save_rotated_token(replacement, expires_at)
        self.clear_pending_replacement()
        LOG.info("Node credential rotated; expires_at=%s", format_timestamp(expires_at))

    def load_or_create_pending_replacement(self) -> str:
        path = self.config.pending_rotation_path
        if path.is_file():
            values = read_env_file(path)
            pending = values.get("WAVEMESH_PENDING_AGENT_TOKEN", "")
            if not valid_token(pending):
                raise AgentError("Pending rotation token has an invalid format")
            return pending

        pending = generate_token()
        write_env_file(path, {"WAVEMESH_PENDING_AGENT_TOKEN": pending})
        return pending

    def clear_pending_replacement(self) -> None:
        try:
            self.config.pending_rotation_path.unlink()
        except FileNotFoundError:
            pass

    def collect_and_send_observation(self) -> None:
        route_health = run_json_command(["wavemesh", "cascade", "health", "--json"], timeout=90)
        auto_health = run_json_command(["wavemesh", "cascade", "auto", "health", "--json"], timeout=90)
        state = build_observation_state(route_health, auto_health)
        assert_redacted(state)
        self.last_health_state = state

        canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        state_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if self.runtime.get("last_observation_hash") == state_hash:
            return

        observed_at = datetime.now(timezone.utc)
        self.api_json(
            "POST",
            f"internal/v1/nodes/{self.config.node_id}/observations",
            {
                "type": f"node.health.{state_hash[:16]}",
                "schema_version": 1,
                "observed_version": self.config.observed_version,
                "state": state,
                "observed_at": format_timestamp(observed_at),
            },
            expected=(202,),
            headers={"Idempotency-Key": f"node-health-{self.config.node_id}-{state_hash[:32]}"},
        )
        self.runtime["last_observation_hash"] = state_hash
        self.runtime["last_observation_at"] = format_timestamp(observed_at)
        write_json_file(self.config.runtime_path, self.runtime)
        LOG.info(
            "Health observation accepted: node_status=%s healthy_exits=%s/%s",
            state.get("node_status"),
            state.get("healthy_exits"),
            state.get("total_exits"),
        )

    def send_heartbeat(self) -> None:
        state = self.last_health_state
        node_status = str(state.get("node_status", "unknown"))
        status = "active" if node_status == "healthy" else "degraded"
        config = read_json_file(NODE_CONFIG_PATH, default={})
        builder_version = str((config.get("builder") or {}).get("version") or "unknown")
        node_role = str((config.get("node") or {}).get("role") or "unknown")
        capabilities = {
            "mode": "observe_only",
            "command_polling": False,
            "command_execution": False,
            "node_role": node_role,
            "cascade_routes_total": len(state.get("routes") or []),
            "auto_routes_total": len(state.get("auto_routes") or []),
            "healthy_exits": int(state.get("healthy_exits") or 0),
            "total_exits": int(state.get("total_exits") or 0),
        }
        assert_redacted(capabilities)
        self.api_json(
            "POST",
            f"internal/v1/nodes/{self.config.node_id}/heartbeat",
            {
                "agent_version": AGENT_VERSION,
                "builder_version": builder_version,
                "observed_version": self.config.observed_version,
                "status": status,
                "capabilities": capabilities,
                "sent_at": format_timestamp(datetime.now(timezone.utc)),
            },
            expected=(204,),
        )

    def api_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        expected: tuple[int, ...],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request_headers = {
            "Authorization": f"Bearer {self.config.agent_token}",
            "X-WaveVPN-Tenant-Id": self.config.tenant_id,
            "Content-Type": "application/json",
            "User-Agent": f"wavemesh-node-agent/{AGENT_VERSION}",
        }
        if headers:
            request_headers.update(headers)
        req = request.Request(
            f"{self.config.api_base}/{path.lstrip('/')}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.config.request_timeout_seconds) as response:
                status = response.status
                raw = response.read()
        except error.HTTPError as exc:
            problem = parse_problem(exc.read())
            raise ApiError(
                exc.code,
                str(problem.get("code") or "HTTP_ERROR"),
                bool(problem.get("retryable", False)),
                str(problem.get("message") or ""),
            ) from None
        except error.URLError as exc:
            raise ApiError(0, "NETWORK_ERROR", True, str(exc.reason)) from None

        if status not in expected:
            raise ApiError(status, "UNEXPECTED_STATUS", True)
        if not raw:
            return {}
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise AgentError("SaaS returned a non-object JSON response")
        return decoded


def build_observation_state(route_health: dict[str, Any], auto_health: dict[str, Any]) -> dict[str, Any]:
    routes = []
    for route in route_health.get("routes") or []:
        if not isinstance(route, dict):
            continue
        routes.append(
            {
                "route_id": safe_text(route.get("route_id"), 96),
                "exit_id": safe_text(route.get("exit_id"), 96),
                "enabled": bool(route.get("enabled")),
                "status": safe_text(route.get("status"), 32),
                "latency_ms": safe_int(route.get("latency_ms"), 0, 3_600_000),
            }
        )

    auto_routes = []
    for auto_route in auto_health.get("auto_routes") or []:
        if not isinstance(auto_route, dict):
            continue
        auto_routes.append(
            {
                "route_id": safe_text(auto_route.get("id"), 96),
                "enabled": bool(auto_route.get("enabled")),
                "published": bool(auto_route.get("published")),
                "status": safe_text(auto_route.get("status"), 32),
                "strategy": safe_text(auto_route.get("strategy"), 32),
                "healthy_exits": safe_int(auto_route.get("healthy_exits"), 0, 10000),
                "total_exits": safe_int(auto_route.get("total_exits"), 0, 10000),
            }
        )

    healthy_exits = sum(1 for route in routes if route["enabled"] and route["status"] == "healthy")
    total_exits = sum(1 for route in routes if route["enabled"])
    node_status = safe_text(route_health.get("node_status"), 32) or ("healthy" if healthy_exits else "degraded")
    return {
        "mode": "observe_only",
        "node_status": node_status,
        "healthy_exits": healthy_exits,
        "total_exits": total_exits,
        "routes": sorted(routes, key=lambda item: str(item["route_id"])),
        "auto_routes": sorted(auto_routes, key=lambda item: str(item["route_id"])),
    }


def run_json_command(command: list[str], timeout: int) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "LC_ALL": "C.UTF-8"},
    )
    if completed.returncode != 0:
        raise AgentError(f"Command failed with exit code {completed.returncode}: {' '.join(command[:3])}")
    decoded = json.loads(completed.stdout)
    if not isinstance(decoded, dict):
        raise AgentError("WaveMesh CLI returned a non-object JSON response")
    return decoded


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_token(token: str) -> bool:
    if not token.startswith(TOKEN_PREFIX):
        return False
    tail = token[len(TOKEN_PREFIX) :]
    return 32 <= len(tail) <= 128 and all(character.isalnum() or character in "_-" for character in tail)


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise AgentError(f"Agent environment file is missing: {path}")
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AgentError(f"Invalid environment line {number} in {path}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            raise AgentError(f"Invalid environment key on line {number} in {path}")
        parsed = shlex.split(raw_value, posix=True)
        if len(parsed) > 1:
            raise AgentError(f"Invalid environment value on line {number} in {path}")
        values[key] = parsed[0] if parsed else ""
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"{key}={shlex.quote(str(value))}\n" for key, value in sorted(values.items()))
    atomic_write(path, content.encode("utf-8"), 0o600)


def read_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return dict(default)
    decoded = json.loads(path.read_text(encoding="utf-8"))
    return decoded if isinstance(decoded, dict) else dict(default)


def write_json_file(path: Path, value: dict[str, Any]) -> None:
    body = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    atomic_write(path, body, 0o600)


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def assert_redacted(value: Any, depth: int = 0) -> None:
    if depth > 20:
        raise AgentError("Observed state is nested too deeply")
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = "".join(character.lower() for character in str(key) if character.isalnum())
            if any(part in normalized for part in FORBIDDEN_KEY_PARTS):
                raise AgentError(f"Observed state contains a forbidden key: {key}")
            assert_redacted(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            assert_redacted(item, depth + 1)
    elif isinstance(value, str):
        lowered = value.lower()
        if any(prefix in lowered for prefix in ("vless://", "vmess://", "trojan://", "hysteria://", "hysteria2://")):
            raise AgentError("Observed state contains connection material")


def parse_problem(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("error"), dict):
        return value["error"]
    return value


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise AgentError("Timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def bounded_int(value: str | None, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else fallback
    except ValueError:
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def safe_int(value: Any, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def safe_text(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    return str(value)[:maximum]


def require_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise AgentError(f"SaaS response is missing {key}")
    return item


def stop_aware_sleep(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not _STOP:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def handle_stop(_signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WaveMesh observe-only Node Agent")
    parser.add_argument("command", choices=("run", "once", "check"))
    parser.add_argument("--env-file", type=Path, default=Path(os.environ.get("WAVEMESH_AGENT_ENV", DEFAULT_ENV_PATH)))
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    config = AgentConfig.load(args.env_file)
    if args.command == "check":
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": config.mode,
                    "node_id": config.node_id,
                    "tenant_id": config.tenant_id,
                    "token_expires_at": format_timestamp(config.token_expires_at),
                    "agent_version": AGENT_VERSION,
                },
                ensure_ascii=False,
            )
        )
        return 0

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    NodeAgent(config).run(once=args.command == "once")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
