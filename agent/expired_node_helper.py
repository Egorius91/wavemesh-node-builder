#!/usr/bin/env python3
"""Fixed-command host capability for the admitted seven-day expiry pilot.

Only the pinned central machine key may invoke this root-owned executable.
All identifiers and credentials travel through protected stdin, never argv/logs.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import UUID

from node_expired_identity import inspect_expired, private_path
from node_mtls_state import MtlsStateError, atomic_write_json, atomic_write_bytes, read_json_object, unique_json_object, canonical_csr_pem
from node_recovery import RecoveryClient, RECOVERY_TOKEN_PATTERN, require_credential_id, validate_no_sign_proof

CONFIG = Path("/etc/wavemesh-agent/expired-pilot.json")
JOURNAL = Path("/etc/wavemesh-agent/expired-pilot.operation.json")
TOKEN = Path("/etc/wavemesh-agent/recovery.token")
ENV = Path("/etc/wavemesh-agent/agent.env")
INSTALL = Path("/usr/local/lib/wavemesh-agent")
LOCK = Path("/etc/wavemesh-agent/expired-pilot.delivery.lock")
HISTORY = Path("/etc/wavemesh-agent/expired-pilot.operations")
COMMANDS = frozenset(("inspect", "prepare", "deliver", "apply", "reconcile"))
HASH = re.compile(r"^[a-f0-9]{64}$")
BACKUPS = Path("/var/lib/wavemesh-agent/recovery-backups")


def immutable_config_hash(config: dict) -> str:
    immutable = {key: value for key, value in config.items() if key != "enabled"}
    return hashlib.sha256(json.dumps(immutable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def attestation(config: dict) -> dict:
    return {"helperSourceSha256": config["sourceHashes"]["expired_node_helper.py"],
            "configSha256": immutable_config_hash(config)}


def cleanup_evidence(client: RecoveryClient, operation_id: str, *, remove_backup_tokens: bool = False) -> dict:
    private_path(client.completed_file)
    receipt = read_json_object(client.completed_file)
    if receipt.get("operation_id") != operation_id or receipt.get("acknowledged_at") is None:
        raise MtlsStateError("Known ACK receipt is not bound to the operation")
    active = client.state.active_identity(client.expected_identity_uri)
    if active is None or client.state.active_request_hash(active) != receipt.get("request_hash"):
        raise MtlsStateError("Known ACK receipt active identity mismatch")
    credential_id = require_credential_id(receipt, "credential_id")
    absent = lambda path: not path.exists() and not path.is_symlink()
    token_absent = absent(TOKEN)
    pending_absent = absent(client.pending_file)
    ack_absent = absent(client.state.acknowledgement_path)
    private_path(client.runtime_file)
    runtime = read_json_object(client.runtime_file)
    active_service = subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", "wavemesh-node-agent.service"],
                                    capture_output=True, check=False, timeout=10).returncode == 0
    shadow = runtime.get("state") == "SHADOW_ACTIVE" and active_service
    if not (token_absent and pending_absent and ack_absent and shadow):
        raise MtlsStateError("Independent local cleanup acceptance failed")
    private_path(BACKUPS, True)
    backup_tokens_absent = True
    backups = list(BACKUPS.iterdir())
    if len(backups) > 1024:
        raise MtlsStateError("Backup reconciliation requires bounded operator review")
    matched = 0
    for directory in backups:
        private_path(directory, True)
        binding = directory / "pilot.operation.id"
        if not binding.exists() and not binding.is_symlink():
            continue
        private_path(binding)
        if binding.read_text().strip() != operation_id:
            continue
        matched += 1
        token = directory / "recovery.token.before"
        if not absent(token):
            private_path(token)
            if remove_backup_tokens:
                # Committed ACK and active service above are independent prerequisites.
                manifest = directory / "SHA256SUMS"
                private_path(manifest)
                retained = directory / "SHA256SUMS.before-pilot-cleanup"
                if not retained.exists():
                    atomic_write_bytes(retained, manifest.read_bytes(), 0o600)
                token.unlink()
                entries = []
                for path in sorted(directory.iterdir()):
                    if path.name == "SHA256SUMS":
                        continue
                    private_path(path, max_bytes=None)
                    digest = hashlib.sha256()
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(65536), b""):
                            digest.update(chunk)
                    entries.append(digest.hexdigest() + "  ./" + path.name)
                atomic_write_bytes(manifest, ("\n".join(entries) + "\n").encode(), 0o600)
            backup_tokens_absent = backup_tokens_absent and absent(token)
    if not matched or not backup_tokens_absent:
        raise MtlsStateError("Operation backup token cleanup not proved")
    return {"observed_at": datetime.now(timezone.utc).isoformat(), "operation_id": operation_id,
            "credential_id_hash": hashlib.sha256(credential_id.encode()).hexdigest(),
            "local_request_hash": receipt["request_hash"], "token_absent": token_absent,
            "recovery_pending_absent": pending_absent, "ack_pending_absent": ack_absent,
            "backup_token_absent": backup_tokens_absent, "shadow_active": shadow}


def load_config() -> dict:
    private_path(CONFIG.parent, True)
    for path in (INSTALL, INSTALL.parent, Path("/usr/local/sbin"), Path("/usr/local")):
        info = path.lstat()
        if path.is_symlink() or not path.is_dir() or info.st_uid != 0 or info.st_mode & 0o022:
            raise MtlsStateError("Pilot executable directory unsafe")
    private_path(CONFIG)
    config = read_json_object(CONFIG)
    if config.get("schemaVersion") != 1:
        raise MtlsStateError("Pilot configuration version invalid")
    if config.get("compromised") is not False:
        raise MtlsStateError("Pilot compromise requires operator reconciliation")
    start = datetime.fromisoformat(config["activatedAt"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(config["expiresAt"].replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None or not 0 < (end - start).total_seconds() <= 7 * 86400:
        raise MtlsStateError("Pilot authority window invalid")
    for field in ("hostBindingHash", "nodeIdentityHash", "trustedCaBundleSha256"):
        if not HASH.fullmatch(config.get(field, "")):
            raise MtlsStateError("Pilot configuration hash invalid")
    source_files = ("expired_node_helper.py", "node_recovery.py", "node_expired_identity.py", "node_mtls_state.py", "node_agent.py")
    pins = config.get("sourceHashes")
    if not isinstance(pins, dict) or set(pins) != set(source_files) | {"recover.sh", "expired-node-forced-command.sh"}:
        raise MtlsStateError("Pilot source pins incomplete")
    for filename, pin in pins.items():
        path = (Path("/usr/local/sbin/wavemesh-node-agent-recover") if filename == "recover.sh" else
                Path("/usr/local/sbin/expired-node-forced-command") if filename == "expired-node-forced-command.sh" else INSTALL / filename)
        info = path.lstat()
        if not HASH.fullmatch(pin) or path.is_symlink() or not path.is_file() or info.st_uid != 0 or info.st_mode & 0o022 or hashlib.sha256(path.read_bytes()).hexdigest() != pin:
            raise MtlsStateError("Pilot source pin mismatch")
    private_path(ENV)
    client = RecoveryClient(ENV, TOKEN, config["externalId"])
    if config.get("certificateTtlSeconds") != 86400 or config.get("rotateBeforeSeconds") != int(client.values.get("WAVEMESH_AGENT_MTLS_ROTATE_BEFORE_SECONDS", "21600")):
        raise MtlsStateError("Pilot accepted certificate profile mismatch")
    if not 0 < config["rotateBeforeSeconds"] < config["certificateTtlSeconds"] - 3600:
        raise MtlsStateError("Pilot rotation horizon invalid")
    if (client.node_id, client.tenant_id, client.environment, str(client.state_root)) != (config["nodeId"], config["tenantId"], config["environment"], "/etc/wavemesh-agent/tls"):
        raise MtlsStateError("Pilot target binding mismatch")
    return config


def validate_binding(config: dict, data: dict) -> None:
    allowed = {"schemaVersion", "operationId", "hostBindingHash", "nodeIdentityHash", "expiredCredentialId",
               "certificateFingerprintHash", "certificateSerialHash", "publicKeyHash", "certificateExpiresAt",
               "trustedCaBundleSha256", "baselineObservedAt", "acknowledged", "revoked", "quarantined",
               "compromised", "token", "credentialId", "localRequestHash", "pemRequestHash", "noSignProof", "newSignAllowed"}
    if set(data) - allowed:
        raise MtlsStateError("Unexpected pilot message fields")
    require_credential_id(data, "expiredCredentialId")
    if data.get("schemaVersion") != 1:
        raise MtlsStateError("Pilot message version invalid")
    operation = UUID(data["operationId"])
    if operation.version != 4 or str(operation) != data["operationId"]:
        raise MtlsStateError("Pilot operation UUID must be canonical v4")
    for field in ("hostBindingHash", "nodeIdentityHash"):
        if data.get(field) != config[field]:
            raise MtlsStateError("Pilot operation target mismatch")
    if data.get("trustedCaBundleSha256") != config["trustedCaBundleSha256"]:
        raise MtlsStateError("Pilot trust configuration mismatch")
    for field in ("certificateFingerprintHash", "certificateSerialHash", "publicKeyHash", "trustedCaBundleSha256"):
        if not HASH.fullmatch(data.get(field, "")):
            raise MtlsStateError("Pilot baseline hash invalid")
    if not isinstance(data.get("certificateExpiresAt"), str):
        raise MtlsStateError("Pilot expiry timestamp invalid")
    expiry = datetime.fromisoformat(data["certificateExpiresAt"].replace("Z", "+00:00"))
    if expiry.tzinfo is None:
        raise MtlsStateError("Pilot expiry timestamp missing timezone")


def journal() -> dict | None:
    if not JOURNAL.exists() and not JOURNAL.is_symlink():
        return None
    private_path(JOURNAL)
    return read_json_object(JOURNAL)


def admission(config: dict, data: dict, known_no_sign: bool = False) -> dict:
    validate_binding(config, data)
    now = datetime.now(timezone.utc)
    observed = datetime.fromisoformat(data["baselineObservedAt"].replace("Z", "+00:00"))
    if observed.tzinfo is None or not 0 <= (now - observed).total_seconds() <= 600:
        raise MtlsStateError("Pilot baseline observation is stale")
    if (config.get("enabled") is not True or config.get("compromised") is not False
            or not datetime.fromisoformat(config["activatedAt"].replace("Z", "+00:00")) <= now < datetime.fromisoformat(config["expiresAt"].replace("Z", "+00:00"))):
        raise MtlsStateError("Pilot issuance authority inactive")
    client = RecoveryClient(ENV, TOKEN, config["externalId"])
    if (client.pending_file.exists() or client.pending_file.is_symlink()) and not known_no_sign:
        raise MtlsStateError("Existing recovery requires reconciliation")
    result = inspect_expired(client.state, client.expected_identity_uri, data, config["certificateTtlSeconds"] + 3600)
    return {**result, "schemaVersion": 1, "operationId": data["operationId"],
            "hostBindingHash": config["hostBindingHash"], "nodeIdentityHash": config["nodeIdentityHash"],
            "observedAt": datetime.now(timezone.utc).isoformat(), **attestation(config)}


def dispatch(command: str, data: dict) -> dict:
    if command not in COMMANDS:
        raise MtlsStateError("Fixed command invalid")
    config = load_config()
    if config.get("compromised") is not False:
        raise MtlsStateError("Pilot compromise requires operator reconciliation")
    validate_binding(config, data)
    current = journal()
    if command == "inspect":
        if current is not None and current.get("phase") != "ACCEPTED":
            raise MtlsStateError("Outstanding operation requires reconciliation")
        return admission(config, data)
    if command == "prepare":
        if current is not None and current.get("phase") != "ACCEPTED":
            raise MtlsStateError("Outstanding operation requires reconciliation")
        import fcntl
        recovery_lock = Path("/run/lock/wavemesh-node-recovery.lock")
        private_path(recovery_lock)
        with recovery_lock.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            admission(config, data)
            client = RecoveryClient(ENV, TOKEN, config["externalId"])
            client.state.prepare_pending_request()
            return admission(config, data)
    binding = {key: value for key, value in data.items() if key not in ("token", "credentialId", "localRequestHash", "pemRequestHash", "noSignProof", "newSignAllowed")}
    if command == "deliver":
        admission(config, data)
        token = data.get("token")
        if not isinstance(token, str) or not RECOVERY_TOKEN_PATTERN.fullmatch(token):
            raise MtlsStateError("Pilot token invalid")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        if current is not None and current.get("phase") == "ACCEPTED" and current.get("binding") != binding:
            private_path(HISTORY, True)
            old_id = str(UUID(current["binding"]["operationId"]))
            old_path = HISTORY / (old_id + ".json")
            if old_path.exists():
                private_path(old_path)
                if read_json_object(old_path) != current:
                    raise MtlsStateError("Archived operation conflicts")
            else:
                atomic_write_json(old_path, current, 0o600)
            if (HISTORY / (str(UUID(data["operationId"])) + ".json")).exists():
                raise MtlsStateError("Archived operation cannot be dispatched again")
            if current["binding"]["certificateFingerprintHash"] == binding["certificateFingerprintHash"]:
                raise MtlsStateError("Expired credential cannot be issued twice")
            current = None
        if current is not None:
            if current.get("binding") != binding or current.get("tokenHash") != token_hash:
                raise MtlsStateError("Operation or token replacement forbidden")
            if current.get("phase") == "ACCEPTED":
                return {"ok": True, "code": "ACCEPTED", "operationId": data["operationId"], "localRequestHash": current["activeRequestHash"], **attestation(config),
                        "cleanup": cleanup_evidence(RecoveryClient(ENV, TOKEN, config["externalId"]), data["operationId"])}
            if current.get("phase") != "DELIVERED":
                raise MtlsStateError("Token delivery after dispatch is forbidden")
        else:
            admission(config, data)
            if TOKEN.exists() or TOKEN.is_symlink():
                raise MtlsStateError("Unrelated recovery token exists")
            # Journal before token: interrupted delivery may continue only these bytes.
            current = {"binding": binding, "tokenHash": token_hash, "phase": "DELIVERED"}
            atomic_write_json(JOURNAL, current, 0o600)
        if TOKEN.exists() or TOKEN.is_symlink():
            private_path(TOKEN)
            if hashlib.sha256(TOKEN.read_bytes().strip()).hexdigest() != token_hash:
                raise MtlsStateError("Protected token bytes mismatch")
        else:
            atomic_write_bytes(TOKEN, token.encode() + b"\n", 0o600)
        return {"ok": True, "code": "DELIVERED", "operationId": data["operationId"], **attestation(config)}
    if current is None or current.get("binding") != binding:
        raise MtlsStateError("Protected operation binding mismatch")
    client = RecoveryClient(ENV, TOKEN, config["externalId"])
    if current.get("phase") == "ACCEPTED":
        return {"ok": True, "code": "ACCEPTED", "operationId": data["operationId"], "localRequestHash": current["activeRequestHash"], **attestation(config),
                "cleanup": cleanup_evidence(client, data["operationId"])}
    if command == "reconcile":
        marker = client._load_marker(required=False)
        if marker is None and current.get("phase") != "DELIVERED" and client.completed_file.exists():
            private_path(client.completed_file)
            receipt = read_json_object(client.completed_file)
            active = client.state.active_identity(client.expected_identity_uri)
            if (receipt.get("operation_id") != data["operationId"] or receipt.get("acknowledged_at") is None
                    or active is None or client.state.active_request_hash(active) != receipt.get("request_hash")):
                raise MtlsStateError("Committed recovery receipt mismatch")
            client._write_marker(receipt)
            marker = client._load_marker(required=True)
        if marker is not None and marker.get("credential_id") is None:
            if (marker.get("dispatch_state") == "KNOWN_NO_SIGN" or
                    (marker.get("dispatch_state") == "READY" and marker.get("post_dispatched_at") is None and marker.get("reconciled_no_sign_proof"))):
                # Both the exact locally observed final HTTP receipt and the
                # authenticated fresh DB copy are required. Missing fence alone
                # cannot turn an unknown earlier POST into permission to repeat.
                pending = client.state._load_and_validate_pending()
                proof = marker.get("no_sign_proof") or marker.get("reconciled_no_sign_proof")
                validate_no_sign_proof(proof, marker, pending)
                if data.get("newSignAllowed") is not True or data.get("noSignProof") != proof:
                    raise MtlsStateError("Independent no-SIGN receipt readback mismatch")
                admission(config, data, known_no_sign=True)
                client._write_marker({**marker, "dispatch_state": "READY", "post_dispatched_at": None,
                                      "no_sign_proof": None, "reconciled_no_sign_proof": proof})
                marker = client._load_marker(required=True)
            else:
                marker = reconcile_persisted_credential(client, marker, data)
        elif marker is None and current.get("phase") != "DELIVERED":
            raise MtlsStateError("Lost local receipt requires independent reconciliation")
        if marker is None:
            raise MtlsStateError("Reconciliation cannot dispatch a fresh POST")
    if current.get("phase") == "DELIVERED":
        admission(config, data)
    elif command == "apply":
        raise MtlsStateError("Dispatched operation requires reconciliation")
    current["phase"] = "APPLY_DISPATCHED"
    atomic_write_json(JOURNAL, current, 0o600)
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "WAVEMESH_RECOVERY_EXTERNAL_NODE_ID": config["externalId"],
                   "WAVEMESH_EXPIRY_PILOT_ADMISSION": "1", "WAVEMESH_EXPIRY_OPERATION_ID": data["operationId"]}
    # recover.sh alone owns its recovery flock. Delivery lock serializes machine calls.
    completed = subprocess.run(["/usr/local/sbin/wavemesh-node-agent-recover"], env=environment, capture_output=True, timeout=300, check=False)
    if completed.returncode != 0:
        marker = client._load_marker(required=False)
        if marker is None and completed.stderr.strip() == b"NODE_RECOVERY_ERROR=recovery_already_running":
            current["phase"] = "DELIVERED"
            atomic_write_json(JOURNAL, current, 0o600)
            return {"ok": False, "code": "RECOVERY_LOCK_BUSY", "operationId": data["operationId"], "retry_safe": False}
        known = bool(marker and marker.get("credential_id"))
        code = "KNOWN_NO_SIGN_RECONCILE" if marker and marker.get("dispatch_state") == "KNOWN_NO_SIGN" else "KNOWN_CREDENTIAL_RECONCILE" if known else "SIGN_OUTCOME_UNKNOWN"
        return {"ok": False, "code": code, "operationId": data["operationId"], "retry_safe": known}
    if TOKEN.exists() or client.pending_file.exists() or client.state.acknowledgement_path.exists():
        raise MtlsStateError("Recovery cleanup incomplete")
    active = client.state.active_identity(client.expected_identity_uri)
    if active is None:
        raise MtlsStateError("Recovery active identity missing")
    current["phase"] = "ACCEPTED"
    current["activeRequestHash"] = client.state.active_request_hash(active)
    cleanup = cleanup_evidence(client, data["operationId"], remove_backup_tokens=True)
    current["cleanup"] = cleanup
    atomic_write_json(JOURNAL, current, 0o600)
    return {"ok": True, "code": "ACCEPTED", "operationId": data["operationId"], "localRequestHash": current["activeRequestHash"], "cleanup": cleanup, **attestation(config)}


def reconcile_persisted_credential(client: RecoveryClient, marker: dict, data: dict) -> dict:
    # Trusted central DB receipt only. No inferred status, no new POST.
    credential = require_credential_id(data, "credentialId")
    local_hash = data.get("localRequestHash")
    pem_hash = data.get("pemRequestHash")
    if marker.get("operation_id") != data["operationId"] or local_hash != marker["request_hash"] or not HASH.fullmatch(pem_hash or ""):
        raise MtlsStateError("Persisted receipt CSR binding mismatch")
    pending = client.state._load_and_validate_pending()
    canonical_pem = canonical_csr_pem(pending.csr_pem)
    if hashlib.sha256(canonical_pem.encode()).hexdigest() != pem_hash:
        raise MtlsStateError("Persisted receipt canonical CSR binding mismatch")
    client._write_marker(client._bind_credential(marker, credential))
    return client._load_marker(required=True)


def wrapper_admission() -> None:
    """Called by recover.sh after acquiring recovery flock, before stopping Agent."""
    config = load_config()
    current = journal()
    if current is None or current.get("phase") != "APPLY_DISPATCHED":
        raise MtlsStateError("Pilot operation is not dispatched")
    client = RecoveryClient(ENV, TOKEN, config["externalId"])
    if client.pending_file.exists():
        marker = client._load_marker(required=True)
        if marker.get("operation_id") != current["binding"]["operationId"]:
            raise MtlsStateError("Unknown issuance requires reconciliation")
        if marker.get("credential_id") is None:
            if marker.get("dispatch_state") != "READY" or marker.get("post_dispatched_at") is not None or not marker.get("reconciled_no_sign_proof"):
                raise MtlsStateError("Unknown issuance requires reconciliation")
            validate_no_sign_proof(marker["reconciled_no_sign_proof"], marker, client.state._load_and_validate_pending())
            admission(config, current["binding"], known_no_sign=True)
        client.check()
    else:
        admission(config, current["binding"])


def main() -> int:
    try:
        if os.geteuid() != 0 or len(sys.argv) != 2:
            raise MtlsStateError("Root fixed command required")
        command = sys.argv[1]
        if command == "wrapper-admission":
            wrapper_admission()
            return 0
        if command not in COMMANDS:
            raise MtlsStateError("Fixed command invalid")
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise MtlsStateError("Pilot message too large")
        data = json.loads(raw, object_pairs_hook=unique_json_object)
        if not isinstance(data, dict):
            raise MtlsStateError("Pilot message invalid")
        import fcntl
        private_path(LOCK)
        with LOCK.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = dispatch(command, data)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 1
    except Exception:
        # Deliberately fixed: OpenSSL errors and config/HTTP exceptions may contain IDs.
        print(json.dumps({"ok": False, "code": "STOP_RECONCILIATION_REQUIRED"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
