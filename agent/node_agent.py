#!/usr/bin/env python3
"""WaveMesh Node Agent with opt-in access lifecycle execution.

The agent sends redacted heartbeat and topology health observations to WaveVPN
SaaS. Access command execution is disabled by default and uses authenticated
mTLS when explicitly enabled. Replacement bearer tokens are generated locally;
SaaS receives only their SHA-256 hashes.
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
from urllib import error, parse, request

try:
    from node_mtls_runtime import MtlsRuntimeConfig, NodeMtlsRuntime
except ImportError:  # Preserve bearer-only compatibility during staged upgrades.
    MtlsRuntimeConfig = None  # type: ignore[assignment,misc]
    NodeMtlsRuntime = None  # type: ignore[assignment,misc]

AGENT_VERSION = "0.4.0-access-lifecycle"
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
ROTATION_RETRY_KEYS = (
    "rotation_retry_attempts",
    "rotation_retry_at",
    "rotation_retry_credential_expires_at",
    "rotation_retry_code",
    "rotation_retryable",
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
    rotation_jitter_seconds: int
    rotation_retry_base_seconds: int
    rotation_retry_max_seconds: int
    request_timeout_seconds: int
    runtime_path: Path
    mtls_mode: str
    mtls_api_base: str | None
    mtls_environment: str
    mtls_state_root: Path
    mtls_server_ca_file: Path | None
    mtls_rotate_before_seconds: int
    mtls_retry_base_seconds: int
    mtls_retry_max_seconds: int
    mtls_retry_max_attempts: int
    mtls_retry_jitter_seconds: int
    command_mode: str
    access_runtime_path: Path
    access_state_root: Path

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

        retry_base = bounded_int(
            values.get("WAVEMESH_AGENT_ROTATION_RETRY_BASE_SECONDS"),
            30,
            5,
            300,
        )
        retry_max = max(
            retry_base,
            bounded_int(
                values.get("WAVEMESH_AGENT_ROTATION_RETRY_MAX_SECONDS"),
                900,
                30,
                3600,
            ),
        )
        mtls_mode = values.get("WAVEMESH_AGENT_MTLS_MODE", "disabled")
        if mtls_mode not in {"disabled", "shadow"}:
            raise AgentError("WAVEMESH_AGENT_MTLS_MODE must be disabled or shadow")
        mtls_api_base = values.get("WAVEMESH_AGENT_MTLS_API_BASE") or None
        if mtls_mode == "shadow" and (
            not mtls_api_base
            or not mtls_api_base.startswith("https://")
        ):
            raise AgentError("Shadow mode requires an HTTPS WAVEMESH_AGENT_MTLS_API_BASE")
        command_mode = values.get("WAVEMESH_AGENT_COMMAND_MODE", "disabled")
        if command_mode not in {"disabled", "access"}:
            raise AgentError("WAVEMESH_AGENT_COMMAND_MODE must be disabled or access")
        if command_mode == "access" and mtls_mode != "shadow":
            raise AgentError("Access command mode requires WAVEMESH_AGENT_MTLS_MODE=shadow")
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
            rotation_jitter_seconds=bounded_int(
                values.get("WAVEMESH_AGENT_ROTATION_JITTER_SECONDS"),
                0,
                0,
                3600,
            ),
            rotation_retry_base_seconds=retry_base,
            rotation_retry_max_seconds=retry_max,
            request_timeout_seconds=bounded_int(values.get("WAVEMESH_AGENT_REQUEST_TIMEOUT_SECONDS"), 30, 5, 120),
            runtime_path=Path(values.get("WAVEMESH_AGENT_RUNTIME_PATH", str(DEFAULT_RUNTIME_PATH))),
            mtls_mode=mtls_mode,
            mtls_api_base=mtls_api_base.rstrip("/") if mtls_api_base else None,
            mtls_environment=values.get("WAVEMESH_AGENT_MTLS_ENVIRONMENT", "staging"),
            mtls_state_root=Path(
                values.get(
                    "WAVEMESH_AGENT_MTLS_STATE_ROOT",
                    "/etc/wavemesh-agent/tls",
                )
            ),
            mtls_server_ca_file=(
                Path(values["WAVEMESH_AGENT_MTLS_SERVER_CA_FILE"])
                if values.get("WAVEMESH_AGENT_MTLS_SERVER_CA_FILE")
                else None
            ),
            mtls_rotate_before_seconds=bounded_int(
                values.get("WAVEMESH_AGENT_MTLS_ROTATE_BEFORE_SECONDS"),
                6 * 60 * 60,
                15 * 60,
                3 * 24 * 60 * 60,
            ),
            mtls_retry_base_seconds=bounded_int(
                values.get("WAVEMESH_AGENT_MTLS_RETRY_BASE_SECONDS"),
                30,
                5,
                300,
            ),
            mtls_retry_max_seconds=bounded_int(
                values.get("WAVEMESH_AGENT_MTLS_RETRY_MAX_SECONDS"),
                900,
                30,
                3600,
            ),
            mtls_retry_max_attempts=bounded_int(
                values.get("WAVEMESH_AGENT_MTLS_RETRY_MAX_ATTEMPTS"),
                8,
                1,
                32,
            ),
            mtls_retry_jitter_seconds=bounded_int(
                values.get("WAVEMESH_AGENT_MTLS_RETRY_JITTER_SECONDS"),
                0,
                0,
                300,
            ),
            command_mode=command_mode,
            access_runtime_path=Path(
                values.get(
                    "WAVEMESH_AGENT_ACCESS_RUNTIME_PATH",
                    "/usr/local/lib/wavemesh-agent/access_runtime.py",
                )
            ),
            access_state_root=Path(
                values.get(
                    "WAVEMESH_AGENT_ACCESS_STATE_ROOT",
                    "/var/lib/wavemesh-agent/access",
                )
            ),
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
    def __init__(self, config: AgentConfig, mtls_runtime: Any | None = None) -> None:
        self.config = config
        self.mtls_runtime = mtls_runtime
        self.last_mtls_status: dict[str, Any] = {
            "mode": "disabled",
            "state": "BEARER_ONLY",
            "retry_attempts": 0,
            "retry_at": None,
            "code": None,
        }
        self.last_health_state: dict[str, Any] = {
            "mode": "observe_only",
            "node_status": "unknown",
            "healthy_exits": 0,
            "total_exits": 0,
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
            except ApiError as exc:
                LOG.warning(
                    "Credential rotation failed: status=%s code=%s retryable=%s",
                    exc.status,
                    exc.code,
                    exc.retryable,
                )
            except Exception as exc:  # noqa: BLE001 - long-running service boundary
                LOG.exception("Credential rotation cycle failed: %s", exc)

            self.run_mtls_lifecycle()
            self.run_access_command_cycle()

            if time.monotonic() >= next_observation:
                try:
                    self.collect_and_send_observation()
                    next_observation = time.monotonic() + self.config.observation_seconds
                except ApiError as exc:
                    self.mark_health_degraded()
                    next_observation = time.monotonic() + min(60, self.config.observation_seconds)
                    LOG.warning(
                        "Health observation failed: status=%s code=%s retryable=%s",
                        exc.status,
                        exc.code,
                        exc.retryable,
                    )
                except Exception as exc:  # noqa: BLE001 - long-running service boundary
                    self.mark_health_degraded()
                    next_observation = time.monotonic() + min(60, self.config.observation_seconds)
                    LOG.exception("Health observation cycle failed: %s", exc)

            heartbeat_payload = self.build_heartbeat_payload()
            try:
                self.send_heartbeat(heartbeat_payload)
            except ApiError as exc:
                LOG.warning("Heartbeat failed: status=%s code=%s retryable=%s", exc.status, exc.code, exc.retryable)
            except Exception as exc:  # noqa: BLE001 - long-running service boundary
                LOG.exception("Heartbeat cycle failed: %s", exc)
            self.run_mtls_shadow_heartbeat(heartbeat_payload)

            if once:
                return
            elapsed = time.monotonic() - started
            stop_aware_sleep(max(1.0, self.config.heartbeat_seconds - elapsed))

    def mark_health_degraded(self) -> None:
        self.last_health_state = {
            **self.last_health_state,
            "node_status": "degraded",
        }

    def run_mtls_lifecycle(self) -> None:
        if self.mtls_runtime is None:
            return
        try:
            status = self.mtls_runtime.lifecycle_cycle(AGENT_VERSION)
            self._record_mtls_status(status.capability())
        except Exception as exc:  # noqa: BLE001 - bearer health must remain independent
            self._record_mtls_status(
                {
                    "mode": "shadow",
                    "state": "BLOCKED",
                    "retry_attempts": 0,
                    "retry_at": None,
                    "code": safe_error_code(type(exc).__name__),
                }
            )

    def run_mtls_shadow_heartbeat(self, payload: dict[str, Any]) -> None:
        if self.mtls_runtime is None:
            return
        try:
            status = self.mtls_runtime.shadow_heartbeat(payload)
            self._record_mtls_status(status.capability())
        except Exception as exc:  # noqa: BLE001 - bearer health must remain independent
            self._record_mtls_status(
                {
                    "mode": "shadow",
                    "state": "BLOCKED",
                    "retry_attempts": 0,
                    "retry_at": None,
                    "code": safe_error_code(type(exc).__name__),
                }
            )

    def run_access_command_cycle(self) -> None:
        if self.config.command_mode != "access":
            return
        if self.mtls_runtime is None or self.last_mtls_status.get("state") != "SHADOW_ACTIVE":
            return
        command: dict[str, Any] | None = None
        try:
            command = self.mtls_runtime.api_json(
                "GET",
                f"internal/v1/nodes/{self.config.node_id}/commands/next",
                None,
                expected=(200, 204),
            )
            if not command:
                return
            command_id, attempt, payload = validate_access_command(command, self.config.node_id)
            self.mtls_runtime.api_json(
                "POST",
                f"internal/v1/nodes/{self.config.node_id}/commands/{command_id}/started",
                {},
                expected=(204,),
            )
            material = self.execute_access_runtime(payload)
            self.mtls_runtime.api_json(
                "POST",
                f"internal/v1/nodes/{self.config.node_id}/accesses/{payload['access_id']}/materialize",
                material,
                expected=(200,),
            )
            self.mtls_runtime.api_json(
                "POST",
                f"internal/v1/nodes/{self.config.node_id}/commands/{command_id}/result",
                {
                    "status": "succeeded",
                    "attempt": attempt,
                    "observed_version": payload["desired_version"],
                    "redacted_result": {"access_materialized": True},
                    "completed_at": format_timestamp(datetime.now(timezone.utc)),
                },
                expected=(204,),
            )
            LOG.info("Access provisioning command completed")
        except Exception as exc:  # noqa: BLE001 - isolate command work from health loop
            LOG.warning("Access provisioning command failed: code=%s", safe_error_code(type(exc).__name__))
            if command:
                self.report_access_command_failure(command, safe_error_code(type(exc).__name__))

    def execute_access_runtime(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.config.access_runtime_path.is_file():
            raise AgentError("Access runtime is not installed")
        work_root = self.config.env_path.parent
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".access-command.", dir=work_root) as directory:
            os.chmod(directory, 0o700)
            request_path = Path(directory) / "request.json"
            output_path = Path(directory) / "material.json"
            write_json_file(request_path, payload)
            completed = subprocess.run(
                [
                    "/usr/bin/python3",
                    str(self.config.access_runtime_path),
                    "--request",
                    str(request_path),
                    "--state-root",
                    str(self.config.access_state_root),
                    "--output",
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=90,
                env={**os.environ, "LC_ALL": "C.UTF-8"},
            )
            if completed.returncode != 0 or not output_path.is_file():
                raise AgentError("Access runtime failed")
            material = read_json_file(output_path, default={})
            validate_access_material(material, payload["desired_version"])
            return material

    def report_access_command_failure(self, command: dict[str, Any], code: str) -> None:
        try:
            command_id = safe_id(command.get("command_id"), "command_id")
            attempt = safe_int(command.get("attempt"), 1, 1_000_000)
            payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
            desired_version = safe_int(payload.get("desired_version"), 1, 2_147_483_647)
            self.mtls_runtime.api_json(
                "POST",
                f"internal/v1/nodes/{self.config.node_id}/commands/{command_id}/result",
                {
                    "status": "retryable_failed",
                    "attempt": attempt,
                    "observed_version": desired_version,
                    "error_code": code,
                    "redacted_result": {"access_materialized": False},
                    "completed_at": format_timestamp(datetime.now(timezone.utc)),
                },
                expected=(204,),
            )
        except Exception:  # noqa: BLE001 - the next lease expiry remains the recovery path
            LOG.warning("Could not report access command failure")

    def _record_mtls_status(self, status: dict[str, Any]) -> None:
        assert_redacted(status)
        if status == self.last_mtls_status:
            return
        self.last_mtls_status = dict(status)
        LOG.info(
            "mTLS shadow state changed: state=%s retry_attempts=%s code=%s",
            safe_text(status.get("state"), 32),
            safe_int(status.get("retry_attempts"), 0, 32),
            safe_text(status.get("code"), 96) or "none",
        )

    def rotation_schedule_jitter_seconds(self) -> int:
        return deterministic_jitter_seconds(
            self.config.node_id,
            self.config.token_expires_at,
            self.config.rotation_jitter_seconds,
            namespace="rotation-schedule",
        )

    def rotation_due_at(self) -> datetime:
        return self.config.token_expires_at - timedelta(seconds=self.config.rotate_before_seconds) + timedelta(
            seconds=self.rotation_schedule_jitter_seconds()
        )

    def rotation_is_due(self, now: datetime) -> bool:
        if self.config.pending_rotation_path.is_file():
            return True
        return now >= self.rotation_due_at()

    def rotate_if_due(self) -> None:
        now = datetime.now(timezone.utc)
        self.record_rotation_schedule()
        if not self.rotation_is_due(now):
            return

        retry_at = self.rotation_retry_at()
        if retry_at and now < retry_at:
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
            if exc.code == "NODE_CREDENTIAL_ROTATION_NOT_DUE":
                self.schedule_rotation_retry(now, exc.code, True)
                return
            if exc.code in {
                "NODE_CREDENTIAL_HASH_CONFLICT",
                "NODE_CREDENTIAL_REUSE_FORBIDDEN",
                "NODE_CREDENTIAL_NOT_ACTIVE",
            }:
                self.clear_pending_replacement()
            self.schedule_rotation_retry(now, exc.code, exc.retryable)
            raise
        except Exception as exc:  # noqa: BLE001 - retry boundary
            self.schedule_rotation_retry(now, type(exc).__name__, True)
            raise

        expires_at = parse_timestamp(require_string(response, "expires_at"))
        self.config.save_rotated_token(replacement, expires_at)
        self.clear_pending_replacement()
        self.clear_rotation_retry_state()
        self.record_rotation_schedule()
        LOG.info("Node credential rotated; expires_at=%s", format_timestamp(expires_at))

    def record_rotation_schedule(self) -> None:
        expires_at = format_timestamp(self.config.token_expires_at)
        due_at = format_timestamp(self.rotation_due_at())
        jitter = self.rotation_schedule_jitter_seconds()
        changed = any(
            (
                self.runtime.get("rotation_credential_expires_at") != expires_at,
                self.runtime.get("rotation_due_at") != due_at,
                self.runtime.get("rotation_jitter_seconds") != jitter,
            )
        )
        if not changed:
            return
        self.runtime["rotation_credential_expires_at"] = expires_at
        self.runtime["rotation_due_at"] = due_at
        self.runtime["rotation_jitter_seconds"] = jitter
        write_json_file(self.config.runtime_path, self.runtime)

    def rotation_retry_at(self) -> datetime | None:
        current_expiry = format_timestamp(self.config.token_expires_at)
        if self.runtime.get("rotation_retry_credential_expires_at") != current_expiry:
            self.clear_rotation_retry_state()
            return None
        raw = self.runtime.get("rotation_retry_at")
        if not isinstance(raw, str):
            return None
        try:
            return parse_timestamp(raw)
        except (AgentError, ValueError):
            self.clear_rotation_retry_state()
            return None

    def schedule_rotation_retry(self, now: datetime, code: str, retryable: bool) -> None:
        current_expiry = format_timestamp(self.config.token_expires_at)
        previous_attempts = 0
        if self.runtime.get("rotation_retry_credential_expires_at") == current_expiry:
            previous_attempts = safe_int(self.runtime.get("rotation_retry_attempts"), 0, 1_000_000)
        attempts = previous_attempts + 1
        if retryable:
            delay = rotation_backoff_seconds(
                self.config.rotation_retry_base_seconds,
                self.config.rotation_retry_max_seconds,
                attempts,
                self.config.node_id,
                self.config.token_expires_at,
            )
        else:
            delay = self.config.rotation_retry_max_seconds
        retry_at = now + timedelta(seconds=delay)
        self.runtime.update(
            {
                "rotation_retry_attempts": attempts,
                "rotation_retry_at": format_timestamp(retry_at),
                "rotation_retry_credential_expires_at": current_expiry,
                "rotation_retry_code": safe_text(code, 96),
                "rotation_retryable": bool(retryable),
            }
        )
        write_json_file(self.config.runtime_path, self.runtime)
        LOG.info(
            "Credential rotation retry scheduled: attempts=%s delay_seconds=%s retryable=%s",
            attempts,
            delay,
            retryable,
        )

    def clear_rotation_retry_state(self) -> None:
        changed = False
        for key in ROTATION_RETRY_KEYS:
            if key in self.runtime:
                del self.runtime[key]
                changed = True
        if changed:
            write_json_file(self.config.runtime_path, self.runtime)

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

    def build_heartbeat_payload(self) -> dict[str, Any]:
        state = self.last_health_state
        node_status = str(state.get("node_status", "unknown"))
        status = "active" if node_status == "healthy" else "degraded"
        config = read_json_file(NODE_CONFIG_PATH, default={})
        builder_version = str((config.get("builder") or {}).get("version") or "unknown")
        node_role = str((config.get("node") or {}).get("role") or "unknown")
        command_ready = (
            self.config.command_mode == "access"
            and self.last_mtls_status.get("state") == "SHADOW_ACTIVE"
        )
        capabilities = {
            "mode": "access_lifecycle" if self.config.command_mode == "access" else "observe_only",
            "command_polling": command_ready,
            "command_execution": command_ready,
            "access_lifecycle": command_ready,
            "node_role": node_role,
            "cascade_routes_total": len(state.get("routes") or []),
            "auto_routes_total": len(state.get("auto_routes") or []),
            "healthy_exits": int(state.get("healthy_exits") or 0),
            "total_exits": int(state.get("total_exits") or 0),
            "mtls_shadow": self.last_mtls_status,
        }
        assert_redacted(capabilities)
        return {
            "agent_version": AGENT_VERSION,
            "builder_version": builder_version,
            "observed_version": self.config.observed_version,
            "status": status,
            "capabilities": capabilities,
            "sent_at": format_timestamp(datetime.now(timezone.utc)),
        }

    def send_heartbeat(self, payload: dict[str, Any] | None = None) -> None:
        self.api_json(
            "POST",
            f"internal/v1/nodes/{self.config.node_id}/heartbeat",
            payload or self.build_heartbeat_payload(),
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


def deterministic_jitter_seconds(
    node_id: str,
    expires_at: datetime,
    maximum: int,
    namespace: str,
    attempt: int = 0,
) -> int:
    if maximum <= 0:
        return 0
    material = "\0".join(
        (
            namespace,
            node_id,
            format_timestamp(expires_at),
            str(max(0, attempt)),
        )
    ).encode("utf-8")
    value = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return value % (maximum + 1)


def rotation_backoff_seconds(
    base_seconds: int,
    max_seconds: int,
    attempt: int,
    node_id: str,
    expires_at: datetime,
) -> int:
    bounded_attempt = max(1, attempt)
    exponent = min(20, bounded_attempt - 1)
    ceiling = min(max_seconds, base_seconds * (2**exponent))
    floor = max(1, ceiling // 2)
    spread = max(0, ceiling - floor)
    return floor + deterministic_jitter_seconds(
        node_id,
        expires_at,
        spread,
        namespace="rotation-retry",
        attempt=bounded_attempt,
    )


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


def safe_error_code(value: Any) -> str:
    normalized = "".join(
        character if character.isalnum() else "_"
        for character in str(value).upper()
    )[:96]
    return normalized if normalized and normalized[0].isalpha() else "MTLS_INTERNAL_ERROR"


def build_mtls_runtime(config: AgentConfig) -> Any | None:
    if config.mtls_mode == "disabled":
        return None
    if NodeMtlsRuntime is None or MtlsRuntimeConfig is None:
        raise AgentError("Node Agent mTLS runtime modules are not installed")
    return NodeMtlsRuntime(
        MtlsRuntimeConfig(
            mode=config.mtls_mode,
            bearer_api_base=config.api_base,
            mtls_api_base=config.mtls_api_base,
            node_id=config.node_id,
            tenant_id=config.tenant_id,
            environment=config.mtls_environment,
            bearer_token=config.agent_token,
            state_root=config.mtls_state_root,
            server_ca_file=config.mtls_server_ca_file,
            request_timeout_seconds=config.request_timeout_seconds,
            rotate_before_seconds=config.mtls_rotate_before_seconds,
            retry_base_seconds=config.mtls_retry_base_seconds,
            retry_max_seconds=max(
                config.mtls_retry_base_seconds,
                config.mtls_retry_max_seconds,
            ),
            retry_max_attempts=config.mtls_retry_max_attempts,
            retry_jitter_seconds=config.mtls_retry_jitter_seconds,
        )
    )


def require_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise AgentError(f"SaaS response is missing {key}")
    return item


def safe_id(value: Any, name: str) -> str:
    item = str(value or "")
    if not 8 <= len(item) <= 128 or not all(character.isalnum() or character in "_-" for character in item):
        raise AgentError(f"{name} is invalid")
    return item


def validate_access_command(
    command: dict[str, Any],
    node_id: str,
) -> tuple[str, int, dict[str, Any]]:
    if command.get("type") != "access.provision":
        raise AgentError("Unsupported Node command type")
    if command.get("target_node_id") != node_id or command.get("schema_version") != 1:
        raise AgentError("Node command target or schema is invalid")
    command_id = safe_id(command.get("command_id"), "command_id")
    attempt = safe_int(command.get("attempt"), 1, 1_000_000)
    if attempt != command.get("attempt"):
        raise AgentError("Node command attempt is invalid")
    payload = command.get("payload")
    if not isinstance(payload, dict) or set(payload) != {
        "access_id",
        "desired_version",
        "enabled",
        "expires_at",
        "device_limit",
        "quota_bytes",
    }:
        raise AgentError("Access command payload is invalid")
    if payload.get("enabled") is not True:
        raise AgentError("Disabled access provisioning is unsupported")
    safe_id(payload.get("access_id"), "access_id")
    desired_version = safe_int(payload.get("desired_version"), 1, 2_147_483_647)
    if desired_version != payload.get("desired_version"):
        raise AgentError("Access desired version is invalid")
    parse_timestamp(require_string(payload, "expires_at"))
    if isinstance(payload.get("device_limit"), bool):
        raise AgentError("Access device limit is invalid")
    if safe_int(payload.get("device_limit"), 0, 10_000) != payload.get("device_limit"):
        raise AgentError("Access device limit is invalid")
    quota = payload.get("quota_bytes")
    if not isinstance(quota, str) or not quota.isdigit() or int(quota) > 9_223_372_036_854_775_807:
        raise AgentError("Access quota is invalid")
    return command_id, attempt, dict(payload)


def validate_access_material(material: dict[str, Any], desired_version: int) -> None:
    required = {
        "desired_version",
        "panel_email",
        "client_uuid",
        "sub_id",
        "primary_inbound_id",
        "protocol",
        "subscription_url",
    }
    if set(material) != required or material.get("desired_version") != desired_version:
        raise AgentError("Access runtime output is invalid")
    safe_id(material.get("sub_id"), "sub_id")
    safe_id(material.get("panel_email"), "panel_email")
    try:
        import uuid
        uuid.UUID(str(material.get("client_uuid")), version=4)
    except (ValueError, AttributeError) as exc:
        raise AgentError("Access runtime UUID is invalid") from exc
    if material.get("protocol") != "vless":
        raise AgentError("Access runtime protocol is invalid")
    if safe_int(material.get("primary_inbound_id"), 1, 2_147_483_647) != material.get("primary_inbound_id"):
        raise AgentError("Access runtime inbound is invalid")
    parsed = parse.urlsplit(require_string(material, "subscription_url"))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise AgentError("Access runtime subscription URL is invalid")


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
    mtls_runtime = build_mtls_runtime(config)
    if args.command == "check":
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": config.mode,
                    "node_id": config.node_id,
                    "tenant_id": config.tenant_id,
                    "token_expires_at": format_timestamp(config.token_expires_at),
                    "rotation_jitter_seconds": config.rotation_jitter_seconds,
                    "rotation_retry_base_seconds": config.rotation_retry_base_seconds,
                    "rotation_retry_max_seconds": config.rotation_retry_max_seconds,
                    "mtls_mode": config.mtls_mode,
                    "mtls_configured": mtls_runtime is not None,
                    "mtls_state": (
                        mtls_runtime.status().state.value
                        if mtls_runtime is not None
                        else "BEARER_ONLY"
                    ),
                    "agent_version": AGENT_VERSION,
                },
                ensure_ascii=False,
            )
        )
        return 0

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    NodeAgent(config, mtls_runtime=mtls_runtime).run(once=args.command == "once")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
