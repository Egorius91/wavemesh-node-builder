#!/usr/bin/env python3
"""Staged disabled pilot installation with exact private lineage reconciliation.

Protected operator stdin only. No machine keys are generated. Machine SSH uses
wavemesh-expiry, never root; the forced capability is published last.
"""
from datetime import datetime, timezone
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
from uuid import UUID, uuid4

from node_agent import read_env_file
from node_mtls_state import atomic_write_bytes, atomic_write_json, fsync_directory, read_json_object, unique_json_object
from node_recovery import require_scope_id, validate_external_node_id
from expired_node_helper import immutable_config_hash

HASH = re.compile(r"^[a-f0-9]{64}$")
SOURCES = ("expired_node_helper.py", "node_recovery.py", "node_expired_identity.py", "node_mtls_state.py", "node_agent.py", "recover.sh", "expired-node-forced-command.sh")


class InstallStop(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


class PilotInstaller:
    # root injection is solely for fixtures; main exposes no destination args.
    def __init__(self, source: Path, root: Path = Path("/")) -> None:
        self.source, self.root = source, root
        self.etc = self.path("/etc/wavemesh-agent")
        self.home = self.path("/var/lib/wavemesh-expiry")
        self.install_root = self.path("/var/lib/wavemesh-agent/expired-pilot-install")
        self.journal_path = self.install_root / "journal.json"
        self.config_path = self.etc / "expired-pilot.json"
        self.authorized = self.home / ".ssh" / "authorized_keys"
        self.sudoers = self.path("/etc/sudoers.d/wavemesh-expiry-pilot")
        self.destinations = {name: self.path("/usr/local/lib/wavemesh-agent/" + name) for name in SOURCES if name.endswith(".py")}
        self.destinations.update({"recover.sh": self.path("/usr/local/sbin/wavemesh-node-agent-recover"),
                                  "expired-node-forced-command.sh": self.path("/usr/local/sbin/expired-node-forced-command")})
        self.journal = None

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    def check(self, path: Path, *, directory: bool = False, mode: int | None = None, absent: bool = False) -> None:
        if absent:
            if path.exists() or path.is_symlink():
                raise InstallStop("UNRELATED_CUSTODY_PRESENT")
            return
        info = path.lstat()
        if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or (not directory and info.st_nlink != 1)):
            raise InstallStop("UNSAFE_PATH_TYPE_OR_LINKS")
        if os.name != "nt" and (info.st_uid != 0 or info.st_mode & 0o022):
            if not (directory and path == self.path("/run/lock") and info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                raise InstallStop("UNSAFE_OWNER_OR_WRITERS")
        if mode is not None and os.name != "nt" and stat.S_IMODE(info.st_mode) != mode:
            raise InstallStop("UNSAFE_MODE")
        if not directory and info.st_size > 1024 * 1024:
            raise InstallStop("CUSTODY_FILE_TOO_LARGE")

    def parents(self, path: Path, *, missing: bool = False) -> None:
        for parent in path.parents:
            if parent == self.root.parent:
                break
            if missing and not parent.exists() and not parent.is_symlink():
                continue
            self.check(parent, directory=True)
            if parent == self.root:
                break

    def account(self) -> dict | None:
        import pwd
        import grp
        try:
            row = pwd.getpwnam("wavemesh-expiry")
        except KeyError:
            return None
        return {"uid": row.pw_uid, "gid": row.pw_gid, "home": row.pw_dir, "shell": row.pw_shell,
                "primaryGroupName": grp.getgrgid(row.pw_gid).gr_name,
                "groupIds": sorted(set(os.getgrouplist(row.pw_name, row.pw_gid)))}

    def existing_group(self) -> dict | None:
        import grp
        try:
            row = grp.getgrnam("wavemesh-expiry")
        except KeyError:
            return None
        return {"gid": row.gr_gid, "name": row.gr_name}

    def known_account(self, row: dict | None) -> dict:
        if row is None or row["uid"] <= 0 or row["gid"] <= 0 or row["home"] != str(self.home) or row["shell"] != "/bin/sh":
            raise InstallStop("USER_CREATION_OUTCOME_UNRESOLVED")
        if row.get("primaryGroupName") != "wavemesh-expiry" or row.get("groupIds") != [row["gid"]]:
            raise InstallStop("UNEXPECTED_ACCOUNT_GROUP_AUTHORITY")
        if self.journal.get("accountUid") is not None and (row["uid"], row["gid"]) != (self.journal["accountUid"], self.journal["accountGid"]):
            raise InstallStop("ACCOUNT_IDENTITY_CHANGED")
        return row

    def run_program(self, args: list[str]) -> None:
        result = subprocess.run(args, capture_output=True, check=False, timeout=30,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"})
        if result.returncode != 0:
            raise InstallStop("FIXED_PROGRAM_FAILED")

    def validate_payload(self, payload: dict) -> tuple[dict, bytes, bytes, str]:
        if set(payload) - {"mode", "config", "publicKey", "centralAddress"}:
            raise InstallStop("UNKNOWN_INSTALL_FIELDS")
        config = payload["config"]
        if not isinstance(config, dict) or config.get("schemaVersion") != 1 or config.get("enabled") is not False or config.get("compromised") is not False:
            raise InstallStop("DISABLED_CONFIGURATION_REQUIRED")
        start = datetime.fromisoformat(config["activatedAt"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(config["expiresAt"].replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None or not 0 < (end - start).total_seconds() <= 7 * 86400:
            raise InstallStop("INVALID_PILOT_WINDOW")
        for key in ("hostBindingHash", "nodeIdentityHash", "trustedCaBundleSha256"):
            if not HASH.fullmatch(config.get(key, "")):
                raise InstallStop("INVALID_CONFIG_PIN")
        require_scope_id(config.get("nodeId"), "Node ID")
        require_scope_id(config.get("tenantId"), "Tenant ID")
        validate_external_node_id(config["externalId"])
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", config.get("environment", "")):
            raise InstallStop("INVALID_ENVIRONMENT")
        if config.get("certificateTtlSeconds") != 86400 or not isinstance(config.get("rotateBeforeSeconds"), int) or not 0 < config["rotateBeforeSeconds"] < 82800:
            raise InstallStop("INVALID_ACCEPTED_PROFILE")
        pins = config.get("sourceHashes")
        if not isinstance(pins, dict) or set(pins) != set(SOURCES) or any(not isinstance(pin, str) or not HASH.fullmatch(pin) for pin in pins.values()):
            raise InstallStop("EXPECTED_SOURCE_PINS_REQUIRED")
        key = payload["publicKey"]
        if not isinstance(key, str) or not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]{40,128}={0,2}", key):
            raise InstallStop("INVALID_PUBLIC_KEY")
        wire = base64.b64decode(key.split()[1], validate=True)
        if len(wire) != 51 or wire[:19] != struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32):
            raise InstallStop("INVALID_PUBLIC_KEY_WIRE")
        address = str(ipaddress.ip_address(payload["centralAddress"]))
        authorized = ('from="' + address + '",restrict,command="/usr/bin/sudo -n /usr/local/sbin/expired-node-forced-command \\\"$SSH_ORIGINAL_COMMAND\\\"" ' + key + "\n").encode()
        commands = ", ".join("/usr/local/sbin/expired-node-forced-command " + command for command in ("inspect", "prepare", "deliver", "apply", "reconcile"))
        sudoers = ("wavemesh-expiry ALL=(root) NOPASSWD: " + commands + "\n").encode()
        identity = digest(canonical({"config": config, "publicKey": key, "centralAddress": address}))
        return config, authorized, sudoers, identity

    def preflight(self, config: dict) -> dict:
        # All custody rejections precede writes and every fixed program call.
        self.parents(self.etc)
        self.check(self.etc, directory=True, mode=0o700)
        for path in self.destinations.values():
            self.parents(path)
        self.parents(self.sudoers)
        self.parents(self.home / ".ssh" / "authorized_keys", missing=True)
        self.parents(self.install_root, missing=True)
        self.check(self.path("/var/lib/wavemesh-agent"), directory=True)
        self.check(self.path("/run/lock"), directory=True)
        for program in (self.path("/usr/sbin/visudo"), self.path("/usr/sbin/useradd")):
            self.parents(program)
            self.check(program)
            if os.name != "nt" and not os.access(program, os.X_OK):
                raise InstallStop("FIXED_PROGRAM_NOT_EXECUTABLE")
        for name in SOURCES:
            candidate = self.source / name
            self.parents(candidate)
            self.check(candidate)
            if digest(candidate.read_bytes()) != config["sourceHashes"][name]:
                raise InstallStop("SOURCE_PIN_MISMATCH")
        if self.account() is not None or self.existing_group() is not None:
            raise InstallStop("UNRELATED_ACCOUNT_PRESENT")
        for path in (self.authorized, self.sudoers, self.config_path, self.etc / "expired-pilot.operation.json",
                     self.etc / "expired-pilot.delivery.lock", self.etc / "expired-pilot.operations",
                     self.etc / "recovery.pending", self.etc / "recovery.token", self.etc / "rotation.pending",
                     self.etc / "tls/acknowledgement.pending.json"):
            self.check(path, absent=True)
        if self.home.exists() or self.home.is_symlink():
            self.check(self.home, directory=True, mode=0o755)
            if any(self.home.iterdir()):
                raise InstallStop("UNRELATED_HOME_CONTENT")
        if self.install_root.exists() or self.install_root.is_symlink():
            self.check(self.install_root, directory=True, mode=0o700)
            if any(path.name != "install.lock" for path in self.install_root.iterdir()):
                raise InstallStop("EXISTING_INSTALL_REQUIRES_RECONCILIATION")
        env = self.etc / "agent.env"
        self.check(env, mode=0o600)
        values = read_env_file(env)
        if (values.get("WAVEMESH_NODE_ID"), values.get("WAVEMESH_TENANT_ID"), values.get("WAVEMESH_AGENT_MTLS_ENVIRONMENT", "staging"), values.get("WAVEMESH_AGENT_MTLS_STATE_ROOT", "/etc/wavemesh-agent/tls")) != (config["nodeId"], config["tenantId"], config["environment"], "/etc/wavemesh-agent/tls"):
            raise InstallStop("EXISTING_PROFILE_TARGET_MISMATCH")
        if values.get("WAVEMESH_AGENT_MTLS_MODE") != "shadow" or int(values.get("WAVEMESH_AGENT_MTLS_ROTATE_BEFORE_SECONDS", "21600")) != config["rotateBeforeSeconds"]:
            raise InstallStop("EXISTING_PROFILE_MODE_MISMATCH")
        old = {}
        for name, path in {**self.destinations, "agent.env": env}.items():
            if path.exists() or path.is_symlink():
                self.check(path)
                old[name] = {"present": True, "sha256": digest(path.read_bytes()), "mode": stat.S_IMODE(path.stat().st_mode)}
            else:
                if name in ("node_agent.py", "node_recovery.py", "node_mtls_state.py", "recover.sh", "agent.env"):
                    raise InstallStop("ACCEPTED_NODE_BASELINE_MISSING")
                old[name] = {"present": False}
        recovery_lock = self.path("/run/lock/wavemesh-node-recovery.lock")
        lock_info = None
        if recovery_lock.exists() or recovery_lock.is_symlink():
            self.check(recovery_lock, mode=0o600)
            lock_info = {"inode": recovery_lock.stat().st_ino, "mode": stat.S_IMODE(recovery_lock.stat().st_mode)}
        return {"old": old, "recoveryLock": lock_info}

    def write_journal(self, phase: str | None = None) -> None:
        if phase is not None:
            self.journal["phase"] = phase
            self.journal["status"] = "RUNNING"
        self.journal["updatedAt"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(self.journal_path, self.journal, 0o600)

    def validate_journal(self) -> None:
        allowed = ("PREPARING", "STAGED", "SOURCE_PROMOTING", "SOURCE_PROMOTED", "ACCOUNT_CREATING", "ACCOUNT_CREATED", "CAPABILITY_PROMOTING", "INSTALLED_DISABLED")
        if self.journal.get("phase") not in allowed or set(self.journal.get("old", {})) != set(SOURCES) | {"agent.env"}:
            raise InstallStop("INVALID_FIXED_LINEAGE_FIELDS")
        self.lineage()
        for old in self.journal["old"].values():
            if not isinstance(old, dict) or not isinstance(old.get("present"), bool):
                raise InstallStop("INVALID_OLD_SOURCE_RECEIPT")
            if old["present"] and (not HASH.fullmatch(old.get("sha256", "")) or not isinstance(old.get("mode"), int) or not 0 <= old["mode"] <= 0o777):
                raise InstallStop("INVALID_OLD_SOURCE_RECEIPT")

    def private_directory(self, path: Path, mode: int = 0o700) -> None:
        if path.exists() or path.is_symlink():
            self.check(path, directory=True, mode=mode)
        else:
            self.parents(path)
            path.mkdir(mode=mode)
            os.chmod(path, mode)
            fsync_directory(path.parent)

    def promote(self, target: Path, content: bytes, mode: int, old: dict | None = None) -> None:
        self.parents(target)
        if target.exists() or target.is_symlink():
            self.check(target)
            actual = digest(target.read_bytes())
            if actual == digest(content):
                # Exact already-promoted bytes are reconciled in place; never
                # replace another identical capability inode unnecessarily.
                os.chmod(target, mode)
                self.check(target, mode=mode)
                return
            if actual != digest(content) and (old is None or not old.get("present") or actual != old["sha256"] or stat.S_IMODE(target.stat().st_mode) != old["mode"]):
                raise InstallStop("DESTINATION_CHANGED_AFTER_PREFLIGHT")
        elif old is not None and old.get("present"):
            raise InstallStop("BASELINE_DESTINATION_DISAPPEARED")
        temporary = target.with_name("." + target.name + ".pilot." + self.journal["lineageId"])
        if temporary.exists() or temporary.is_symlink():
            self.check(temporary)
            if digest(temporary.read_bytes()) != digest(content):
                raise InstallStop("PROMOTION_TEMP_CHANGED")
        else:
            atomic_write_bytes(temporary, content, 0o600)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        fsync_directory(target.parent)
        self.check(target, mode=mode)
        if digest(target.read_bytes()) != digest(content):
            raise InstallStop("PROMOTION_READBACK_FAILED")

    def lineage(self) -> Path:
        value = self.journal["lineageId"]
        if str(UUID(value)) != value or UUID(value).version != 4:
            raise InstallStop("INVALID_LINEAGE")
        return self.install_root / value

    def stage(self, config: dict, authorized: bytes, sudoers: bytes) -> None:
        lineage = self.lineage()
        self.private_directory(lineage)
        self.private_directory(lineage / "before")
        self.private_directory(lineage / "staged")
        for name, old in self.journal["old"].items():
            if not old["present"]:
                continue
            backup = lineage / "before" / name
            if backup.exists() or backup.is_symlink():
                self.check(backup, mode=0o600)
                if digest(backup.read_bytes()) != old["sha256"]:
                    raise InstallStop("BACKUP_CHANGED")
            else:
                original = self.etc / "agent.env" if name == "agent.env" else self.destinations[name]
                self.check(original)
                if digest(original.read_bytes()) != old["sha256"] or stat.S_IMODE(original.stat().st_mode) != old["mode"]:
                    raise InstallStop("OLD_SOURCE_CHANGED_BEFORE_BACKUP")
                atomic_write_bytes(backup, original.read_bytes(), 0o600)
        staged = {name: (self.source / name).read_bytes() for name in SOURCES}
        staged.update({"config.json": canonical(config), "authorized_keys": authorized, "sudoers": sudoers})
        for name, content in staged.items():
            if name in SOURCES and digest(content) != config["sourceHashes"][name]:
                raise InstallStop("SOURCE_CHANGED_BEFORE_STAGING")
            path = lineage / "staged" / name
            if path.exists() or path.is_symlink():
                self.check(path, mode=0o600)
                if digest(path.read_bytes()) != digest(content):
                    raise InstallStop("STAGED_FILE_CHANGED")
            else:
                atomic_write_bytes(path, content, 0o600)
        self.run_program(["/usr/sbin/visudo", "-cf", str(lineage / "staged/sudoers")])
        self.journal["stagedHashes"] = {name: digest(content) for name, content in staged.items()}
        self.write_journal("STAGED")

    def verify_staged(self) -> dict[str, bytes]:
        lineage = self.lineage()
        self.check(lineage, directory=True, mode=0o700)
        self.check(lineage / "before", directory=True, mode=0o700)
        self.check(lineage / "staged", directory=True, mode=0o700)
        if set(self.journal["old"]) != set(SOURCES) | {"agent.env"} or set(self.journal["stagedHashes"]) != set(SOURCES) | {"config.json", "authorized_keys", "sudoers"}:
            raise InstallStop("INVALID_FIXED_LINEAGE_FIELDS")
        for name, old in self.journal["old"].items():
            if old["present"]:
                backup = lineage / "before" / name
                self.check(backup, mode=0o600)
                if digest(backup.read_bytes()) != old["sha256"]:
                    raise InstallStop("BACKUP_READBACK_FAILED")
        result = {}
        for name, pin in self.journal["stagedHashes"].items():
            path = lineage / "staged" / name
            self.check(path, mode=0o600)
            result[name] = path.read_bytes()
            if digest(result[name]) != pin:
                raise InstallStop("STAGED_READBACK_FAILED")
        return result

    def assert_disabled(self, content: bytes) -> None:
        self.check(self.config_path, mode=0o600)
        if self.config_path.read_bytes() != content or read_json_object(self.config_path).get("enabled") is not False:
            raise InstallStop("LIVE_CONFIGURATION_NOT_EXACT_DISABLED")

    def verify_live_source(self, staged: dict[str, bytes]) -> None:
        for name, path in self.destinations.items():
            self.parents(path)
            self.check(path, mode=0o755 if name.endswith(".sh") else 0o644)
            if path.read_bytes() != staged[name]:
                raise InstallStop("LIVE_SOURCE_CHANGED")
        profile = self.etc / "agent.env"
        self.check(profile, mode=0o600)
        if digest(profile.read_bytes()) != self.journal["old"]["agent.env"]["sha256"]:
            raise InstallStop("EXISTING_PROFILE_CHANGED")

    def advance(self, config: dict, authorized: bytes, sudoers: bytes) -> dict:
        phase = self.journal["phase"]
        if phase == "PREPARING":
            self.stage(config, authorized, sudoers)
            phase = "STAGED"
        staged = self.verify_staged()
        if phase in ("STAGED", "SOURCE_PROMOTING"):
            self.write_journal("SOURCE_PROMOTING")
            for name, target in self.destinations.items():
                self.promote(target, staged[name], 0o755 if name.endswith(".sh") else 0o644, self.journal["old"][name])
            self.promote(self.config_path, staged["config.json"], 0o600)
            self.private_directory(self.etc / "expired-pilot.operations")
            delivery_lock = self.etc / "expired-pilot.delivery.lock"
            if not delivery_lock.exists() and not delivery_lock.is_symlink():
                descriptor = os.open(delivery_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
                fsync_directory(delivery_lock.parent)
            self.check(delivery_lock, mode=0o600)
            recovery_lock = self.path("/run/lock/wavemesh-node-recovery.lock")
            if not recovery_lock.exists() and not recovery_lock.is_symlink():
                if self.journal["recoveryLock"] is not None:
                    raise InstallStop("RECOVERY_LOCK_DISAPPEARED")
                descriptor = os.open(recovery_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
                fsync_directory(recovery_lock.parent)
            self.check(recovery_lock)
            if self.journal["recoveryLock"] is not None and recovery_lock.stat().st_ino != self.journal["recoveryLock"]["inode"]:
                raise InstallStop("RECOVERY_LOCK_INODE_CHANGED")
            self.write_journal("SOURCE_PROMOTED")
            phase = "SOURCE_PROMOTED"
        if phase == "SOURCE_PROMOTED":
            self.verify_live_source(staged)
            self.assert_disabled(staged["config.json"])
            if self.account() is not None:
                raise InstallStop("ACCOUNT_APPEARED_BEFORE_DISPATCH")
            self.write_journal("ACCOUNT_CREATING")
            self.run_program(["/usr/sbin/useradd", "--system", "--user-group", "--home-dir", str(self.home), "--shell", "/bin/sh", "wavemesh-expiry"])
            phase = "ACCOUNT_CREATING"
        if phase == "ACCOUNT_CREATING":
            row = self.known_account(self.account())
            self.journal["accountUid"] = row["uid"]
            self.journal["accountGid"] = row["gid"]
            self.private_directory(self.home, 0o755)
            self.private_directory(self.home / ".ssh", 0o755)
            self.write_journal("ACCOUNT_CREATED")
            phase = "ACCOUNT_CREATED"
        if phase in ("ACCOUNT_CREATED", "CAPABILITY_PROMOTING"):
            self.verify_live_source(staged)
            self.known_account(self.account())
            self.assert_disabled(staged["config.json"])
            self.write_journal("CAPABILITY_PROMOTING")
            self.promote(self.sudoers, staged["sudoers"], 0o440)
            self.run_program(["/usr/sbin/visudo", "-cf", str(self.sudoers)])
            self.assert_disabled(staged["config.json"])
            self.promote(self.authorized, staged["authorized_keys"], 0o644)
            self.write_journal("INSTALLED_DISABLED")
        return self.readback(staged)

    def readback(self, staged: dict[str, bytes]) -> dict:
        self.assert_disabled(staged["config.json"])
        self.known_account(self.account())
        for name, path in self.destinations.items():
            self.check(path, mode=0o755 if name.endswith(".sh") else 0o644)
            if digest(path.read_bytes()) != self.journal["stagedHashes"][name]:
                raise InstallStop("LIVE_SOURCE_READBACK_FAILED")
        for path, name, mode in ((self.authorized, "authorized_keys", 0o644), (self.sudoers, "sudoers", 0o440)):
            self.parents(path)
            self.check(path, mode=mode)
            if digest(path.read_bytes()) != self.journal["stagedHashes"][name]:
                raise InstallStop("CAPABILITY_READBACK_FAILED")
        env = self.etc / "agent.env"
        self.check(env, mode=0o600)
        if digest(env.read_bytes()) != self.journal["old"]["agent.env"]["sha256"]:
            raise InstallStop("EXISTING_PROFILE_CHANGED")
        self.journal["status"] = "VERIFIED"
        self.journal["receipt"] = {"sourceHashes": {name: self.journal["stagedHashes"][name] for name in SOURCES},
                                   "configSha256": immutable_config_hash(read_json_object(self.config_path)),
                                   "configBytesSha256": self.journal["stagedHashes"]["config.json"],
                                   "authorizedKeysSha256": self.journal["stagedHashes"]["authorized_keys"],
                                   "sudoersSha256": self.journal["stagedHashes"]["sudoers"]}
        self.write_journal()
        return {"ok": True, "code": "INSTALLED_DISABLED", "lineageId": self.journal["lineageId"], **self.journal["receipt"]}

    def execute(self, payload: dict) -> dict:
        config, authorized, sudoers, identity = self.validate_payload(payload)
        mode = payload.get("mode", "install")
        if mode not in ("install", "reconcile", "status"):
            raise InstallStop("INVALID_INSTALL_MODE")
        exists = self.journal_path.exists() or self.journal_path.is_symlink()
        if exists:
            self.parents(self.journal_path)
            self.check(self.journal_path, mode=0o600)
            self.journal = read_json_object(self.journal_path)
            if self.journal.get("payloadHash") != identity or self.journal.get("schemaVersion") != 1:
                raise InstallStop("INSTALL_LINEAGE_BINDING_MISMATCH")
            self.validate_journal()
            if mode == "install":
                raise InstallStop("EXPLICIT_RECONCILIATION_REQUIRED")
            if mode == "status":
                return {"ok": True, "code": "INSTALL_STATUS", "phase": self.journal["phase"], "status": self.journal["status"], "lineageId": self.journal["lineageId"]}
        elif mode != "install":
            raise InstallStop("INSTALL_LINEAGE_MISSING")
        else:
            plan = self.preflight(config)
            self.private_directory(self.install_root)
            self.journal = {"schemaVersion": 1, "lineageId": str(uuid4()), "payloadHash": identity, **plan,
                            "phase": "PREPARING", "status": "RUNNING"}
        lock = self.install_root / "install.lock"
        if not lock.exists() and not lock.is_symlink():
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        self.check(lock, mode=0o600)
        import fcntl
        with lock.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if exists:
                # The original read was before flock: another reconciler may
                # have advanced it. Never overwrite a newer terminal receipt.
                latest = read_json_object(self.journal_path)
                if latest.get("payloadHash") != identity or latest.get("lineageId") != self.journal["lineageId"]:
                    raise InstallStop("LOCKED_LINEAGE_CHANGED")
                self.journal = latest
                self.validate_journal()
            else:
                self.check(self.journal_path, absent=True)
                self.write_journal()
            try:
                return self.advance(config, authorized, sudoers)
            except Exception:
                self.journal["status"] = "STOPPED"
                self.journal["stopCode"] = "INSTALL_RECONCILIATION_REQUIRED"
                self.write_journal()
                raise


def main() -> int:
    try:
        if os.geteuid() != 0 or len(sys.argv) != 1:
            raise InstallStop("ROOT_OPERATOR_REQUIRED")
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise InstallStop("BOUNDED_INSTALL_INPUT_REQUIRED")
        payload = json.loads(raw, object_pairs_hook=unique_json_object)
        if not isinstance(payload, dict):
            raise InstallStop("INSTALL_OBJECT_REQUIRED")
        result = PilotInstaller(Path(__file__).resolve().parent).execute(payload)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"ok": False, "code": "INSTALL_STOP_RECONCILIATION_REQUIRED"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
