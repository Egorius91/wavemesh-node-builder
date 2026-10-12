#!/usr/bin/env python3
"""Isolated custody fixtures; subprocesses/account lookup are simulated only."""
from datetime import datetime, timedelta, timezone
import base64
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
SPEC = importlib.util.spec_from_file_location("expired_installer", ROOT / "agent/install-expired-pilot.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class FixtureInstaller(installer.PilotInstaller):
    def __init__(self, source, root):
        super().__init__(source, root)
        self.row = None
        self.programs = []
        self.useradd_failure = None
        self.bad_groups = False

    def account(self):
        return self.row

    def existing_group(self):
        return None

    def run_program(self, args):
        self.programs.append(args)
        if args[0] == "/usr/sbin/useradd":
            assert self.config_path.exists() and json.loads(self.config_path.read_text())["enabled"] is False
            assert self.journal["phase"] == "ACCOUNT_CREATING"
            if self.useradd_failure == "absent":
                raise installer.InstallStop("SIMULATED_USERADD_LOST_RESULT")
            self.row = {"uid": 5301, "gid": 5301, "home": str(self.home), "shell": "/bin/sh",
                        "primaryGroupName": "wavemesh-expiry", "groupIds": [0, 5301] if self.bad_groups else [5301]}
            if self.useradd_failure == "created":
                raise installer.InstallStop("SIMULATED_USERADD_LOST_RESULT")
        elif args[0] == "/usr/sbin/visudo":
            text = Path(args[-1]).read_text()
            assert "*" not in text
            assert all("expired-node-forced-command " + command in text for command in ("inspect", "prepare", "deliver", "apply", "reconcile"))
        else:
            raise AssertionError("arbitrary program requested")


@unittest.skipUnless(os.name == "nt" or os.geteuid() == 0, "Native custody proof runs with sudo in CI")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for directory, mode in (("etc", 0o755), ("etc/wavemesh-agent", 0o700), ("etc/sudoers.d", 0o755),
                                ("usr", 0o755), ("usr/sbin", 0o755), ("usr/local", 0o755), ("usr/local/lib", 0o755),
                                ("usr/local/lib/wavemesh-agent", 0o755), ("usr/local/sbin", 0o755),
                                ("var", 0o755), ("var/lib", 0o755), ("var/lib/wavemesh-agent", 0o700),
                                ("run", 0o755), ("run/lock", 0o755), ("source", 0o700)):
            (self.root / directory).mkdir(mode=mode)
        for name in ("visudo", "useradd"):
            path = self.root / "usr/sbin" / name
            path.write_text("fixture program: never executed\n")
            os.chmod(path, 0o755)
        self.source = self.root / "source"
        for name in installer.SOURCES:
            path = self.source / name
            path.write_bytes(("accepted candidate " + name + "\n").encode())
            os.chmod(path, 0o600)
        self.subject = FixtureInstaller(self.source, self.root)
        for name, path in self.subject.destinations.items():
            if name in ("node_agent.py", "node_recovery.py", "node_mtls_state.py", "recover.sh"):
                path.write_bytes(("old baseline " + name + "\n").encode())
                os.chmod(path, 0o755 if name.endswith(".sh") else 0o644)
        env = self.subject.etc / "agent.env"
        env.write_text("WAVEMESH_NODE_ID=node_12345678\nWAVEMESH_TENANT_ID=tenant_12345678\n"
                       "WAVEMESH_AGENT_MTLS_MODE=shadow\nWAVEMESH_AGENT_MTLS_ENVIRONMENT=staging\n"
                       "WAVEMESH_AGENT_MTLS_STATE_ROOT=/etc/wavemesh-agent/tls\n")
        os.chmod(env, 0o600)
        now = datetime.now(timezone.utc)
        config = {"schemaVersion": 1, "enabled": False, "compromised": False,
                  "activatedAt": now.isoformat(), "expiresAt": (now + timedelta(days=7)).isoformat(),
                  "nodeId": "node_12345678", "tenantId": "tenant_12345678", "externalId": "entry-fixed",
                  "environment": "staging", "hostBindingHash": "a" * 64, "nodeIdentityHash": "b" * 64,
                  "trustedCaBundleSha256": "c" * 64, "certificateTtlSeconds": 86400, "rotateBeforeSeconds": 21600,
                  "sourceHashes": {name: installer.digest((self.source / name).read_bytes()) for name in installer.SOURCES}}
        wire = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(32)
        self.payload = {"config": config, "publicKey": "ssh-ed25519 " + base64.b64encode(wire).decode(), "centralAddress": "192.0.2.17"}
        self.initial = self.snapshot()
        # Windows has no flock; this local substitute does not claim native proof.
        self.fcntl = mock.patch.dict(sys.modules, {"fcntl": SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *_: None)}) if os.name == "nt" else None
        if self.fcntl:
            self.fcntl.start()

    def tearDown(self):
        if self.fcntl:
            self.fcntl.stop()
        self.temp.cleanup()

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in self.root.rglob("*") if path.is_file() and not path.is_symlink()}

    def test_existing_account_key_sudoers_config_or_pin_rejects_before_any_write(self):
        scenarios = ("account", "authorized", "sudoers", "config", "sourcepin", "lock")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                if scenario == "account":
                    # Good shape alone must never permit reuse of an unowned account.
                    self.subject.row = {"uid": 5301, "gid": 5301, "home": str(self.subject.home), "shell": "/bin/sh",
                                        "primaryGroupName": "wavemesh-expiry", "groupIds": [5301]}
                elif scenario == "sourcepin":
                    self.payload["config"]["sourceHashes"]["node_agent.py"] = "0" * 64
                else:
                    path = {"authorized": self.subject.authorized, "sudoers": self.subject.sudoers,
                            "config": self.subject.config_path, "lock": self.subject.etc / "expired-pilot.delivery.lock"}[scenario]
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    path.write_bytes(b"UNRELATED_CUSTODY\n")
                    os.chmod(path, 0o600)
                before = self.snapshot()
                with self.assertRaises(installer.InstallStop):
                    self.subject.execute(self.payload)
                self.assertEqual(before, self.snapshot())
                self.assertEqual(self.subject.programs, [])
                self.assertFalse(self.subject.install_root.exists())
                if scenario == "account":
                    self.subject.row = None
                elif scenario == "sourcepin":
                    self.payload["config"]["sourceHashes"]["node_agent.py"] = installer.digest((self.source / "node_agent.py").read_bytes())
                else:
                    path.unlink()
                    if scenario == "authorized":
                        path.parent.rmdir()
                        path.parent.parent.rmdir()

    def test_failure_preserves_exact_backups_no_capability_and_reconcile_completes(self):
        original = self.subject.promote
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise installer.InstallStop("SIMULATED_PROMOTION_FAILURE")
            return original(*args, **kwargs)
        with mock.patch.object(self.subject, "promote", side_effect=fail_second):
            with self.assertRaises(installer.InstallStop):
                self.subject.execute(self.payload)
        journal = installer.read_json_object(self.subject.journal_path)
        self.assertEqual(journal["phase"], "SOURCE_PROMOTING")
        self.assertEqual(journal["status"], "STOPPED")
        self.assertFalse(self.subject.authorized.exists())
        self.assertFalse(self.subject.sudoers.exists())
        self.assertFalse(self.subject.config_path.exists())
        before = self.subject.install_root / journal["lineageId"] / "before"
        for name, old in journal["old"].items():
            if old["present"]:
                self.assertEqual(installer.digest((before / name).read_bytes()), old["sha256"])
        result = self.subject.execute({**self.payload, "mode": "reconcile"})
        self.assertEqual(result["code"], "INSTALLED_DISABLED")
        self.assertFalse(installer.read_json_object(self.subject.config_path)["enabled"])
        self.assertEqual(len([args for args in self.subject.programs if args[0] == "/usr/sbin/useradd"]), 1)
        self.assertEqual(self.initial["etc/wavemesh-agent/agent.env"], self.snapshot()["etc/wavemesh-agent/agent.env"])

    def test_useradd_lost_response_known_created_row_reconciles_without_second_dispatch(self):
        self.subject.useradd_failure = "created"
        with self.assertRaises(installer.InstallStop):
            self.subject.execute(self.payload)
        self.assertEqual(installer.read_json_object(self.subject.journal_path)["phase"], "ACCOUNT_CREATING")
        self.assertFalse(self.subject.authorized.exists())
        self.assertFalse(self.subject.sudoers.exists())
        self.assertFalse(installer.read_json_object(self.subject.config_path)["enabled"])
        self.subject.useradd_failure = None
        self.subject.execute({**self.payload, "mode": "reconcile"})
        self.assertEqual(len([args for args in self.subject.programs if args[0] == "/usr/sbin/useradd"]), 1)

    def test_unknown_user_creation_absence_never_retries_or_publishes_key(self):
        self.subject.useradd_failure = "absent"
        with self.assertRaises(installer.InstallStop):
            self.subject.execute(self.payload)
        with self.assertRaisesRegex(installer.InstallStop, "USER_CREATION_OUTCOME_UNRESOLVED"):
            self.subject.execute({**self.payload, "mode": "reconcile"})
        self.assertEqual(len([args for args in self.subject.programs if args[0] == "/usr/sbin/useradd"]), 1)
        self.assertFalse(self.subject.authorized.exists())
        self.assertFalse(self.subject.sudoers.exists())
        self.assertFalse(installer.read_json_object(self.subject.config_path)["enabled"])

    def test_created_account_with_unexpected_groups_stops_before_capability(self):
        self.subject.bad_groups = True
        with self.assertRaisesRegex(installer.InstallStop, "UNEXPECTED_ACCOUNT_GROUP_AUTHORITY"):
            self.subject.execute(self.payload)
        self.assertFalse(self.subject.authorized.exists())
        self.assertFalse(self.subject.sudoers.exists())
        self.assertFalse(installer.read_json_object(self.subject.config_path)["enabled"])

    def test_failure_before_key_publication_preserves_disabled_capability_and_reconciles(self):
        promote = self.subject.promote
        def fail_key(target, *args, **kwargs):
            if target == self.subject.authorized:
                raise installer.InstallStop("SIMULATED_KEY_PROMOTION_FAILURE")
            return promote(target, *args, **kwargs)
        with mock.patch.object(self.subject, "promote", side_effect=fail_key):
            with self.assertRaises(installer.InstallStop):
                self.subject.execute(self.payload)
        self.assertEqual(installer.read_json_object(self.subject.journal_path)["phase"], "CAPABILITY_PROMOTING")
        self.assertFalse(self.subject.authorized.exists())
        self.assertTrue(self.subject.sudoers.exists())
        self.assertFalse(installer.read_json_object(self.subject.config_path)["enabled"])
        self.subject.execute({**self.payload, "mode": "reconcile"})
        self.assertEqual(len([args for args in self.subject.programs if args[0] == "/usr/sbin/useradd"]), 1)

    @unittest.skipIf(os.name == "nt", "Native ownership/hardlink fixture requires Linux")
    def test_writable_owner_or_hardlinked_baseline_rejected_before_any_mutation(self):
        target = self.subject.destinations["node_recovery.py"]
        for scenario in ("writable", "owner", "hardlink"):
            with self.subTest(scenario=scenario):
                if scenario == "writable":
                    os.chmod(target, 0o666)
                elif scenario == "owner":
                    os.chown(target, 65534, 65534)
                else:
                    os.link(target, self.root / "hardlink")
                before = self.snapshot()
                with self.assertRaises(installer.InstallStop):
                    self.subject.execute(self.payload)
                self.assertEqual(before, self.snapshot())
                self.assertEqual(self.subject.programs, [])
                if scenario == "writable":
                    os.chmod(target, 0o644)
                elif scenario == "owner":
                    os.chown(target, 0, 0)
                else:
                    (self.root / "hardlink").unlink()

    def test_terminal_exact_readback_and_changed_payload_are_fail_closed(self):
        result = self.subject.execute(self.payload)
        self.assertEqual(result["code"], "INSTALLED_DISABLED")
        count = len(self.subject.programs)
        with self.assertRaisesRegex(installer.InstallStop, "EXPLICIT_RECONCILIATION"):
            self.subject.execute(self.payload)
        status = self.subject.execute({**self.payload, "mode": "status"})
        self.assertEqual(status["status"], "VERIFIED")
        self.assertEqual(count, len(self.subject.programs))
        with self.assertRaisesRegex(installer.InstallStop, "BINDING_MISMATCH"):
            self.subject.execute({**self.payload, "mode": "reconcile", "centralAddress": "192.0.2.18"})
        again = self.subject.execute({**self.payload, "mode": "reconcile"})
        self.assertEqual(again["lineageId"], result["lineageId"])
        self.assertEqual(count, len(self.subject.programs))


if __name__ == "__main__":
    unittest.main()
