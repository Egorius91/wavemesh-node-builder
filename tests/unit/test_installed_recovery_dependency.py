#!/usr/bin/env python3
"""Installed-only recovery import proof; no TLS keys, HTTP or live paths."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PROGRAM = r'''import hashlib, json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
import node_recovery as recovery
now = datetime.now(timezone.utc).replace(microsecond=0)
start, end = now - timedelta(minutes=1), now + timedelta(days=2)
chain = "synthetic-public-chain\n"
config = Path(sys.argv[2])
config.write_text(json.dumps({"trustedCaBundleSha256": hashlib.sha256(chain.encode()).hexdigest(), "rotateBeforeSeconds": 21600}))
config.chmod(0o600)
class State:
    calls = 0
    def _run_bytes(self, args, input_bytes):
        assert args == ["x509", "-noout", "-startdate", "-enddate"]
        assert input_bytes == b"synthetic-public-certificate"
        self.calls += 1
        return ("notBefore=" + start.strftime("%b %d %H:%M:%S %Y GMT") + "\nnotAfter=" + end.strftime("%b %d %H:%M:%S %Y GMT") + "\n").encode()
client = recovery.RecoveryClient.__new__(recovery.RecoveryClient)
client.state = State()
response = {"credential_id": "credential_12345678", "lifecycle_status": "PENDING_ACKNOWLEDGEMENT",
    "certificate": "synthetic-public-certificate", "chain": chain,
    "not_before": start.isoformat(), "expires_at": end.isoformat(),
    "delivery_expires_at": (now + timedelta(hours=1)).isoformat(),
    "previous_valid_until": None, "already_processed": False, "recovery_reason": "CERTIFICATE_EXPIRED"}
with mock.patch.dict(os.environ, {"WAVEMESH_EXPIRY_PILOT_ADMISSION": "1"}), mock.patch.object(recovery, "Path", return_value=config):
    accepted = client._validate_delivery(response)
assert accepted["credential_id"] == "credential_12345678" and client.state.calls == 1
import node_expired_identity
assert Path(node_expired_identity.__file__).parent == Path(sys.argv[1])
print("installed recovery delivery dependency: PASS")
'''


def prove(installed, scratch):
    return subprocess.run([sys.executable, "-I", "-c", PROGRAM, str(installed), str(scratch / "pilot.json")],
                          cwd=scratch, capture_output=True, text=True)


class InstalledRecoveryDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scratch = Path(self.temp.name)
        self.installed = self.scratch / "installed"
        self.installed.mkdir()
        text = (ROOT / "agent/install.sh").read_text()
        sources = dict(re.findall(r'^([A-Z_]+)="\$PROJECT_DIR/agent/([^"\n]+)"$', text, re.M))
        for name, destination in re.findall(r'^atomic_install_file "\$([A-Z_]+)" "\$INSTALL_DIR/([^"\n]+)"', text, re.M):
            shutil.copyfile(ROOT / "agent" / sources[name], self.installed / destination)

    def test_installed_delivery_reaches_expiry_dependency_without_source_path(self):
        result = prove(self.installed, self.scratch)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("installed recovery delivery dependency: PASS", result.stdout)

    def test_missing_installed_dependency_cannot_fall_back_to_repository(self):
        (self.installed / "node_expired_identity.py").unlink(missing_ok=True)
        result = prove(self.installed, self.scratch)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ModuleNotFoundError", result.stderr)
        self.assertIn("node_expired_identity", result.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed-dir", type=Path)
    args, remaining = parser.parse_known_args()
    if args.installed_dir:
        with tempfile.TemporaryDirectory() as temp:
            result = prove(args.installed_dir.resolve(), Path(temp))
            if result.returncode:
                sys.stderr.write(result.stderr)
            else:
                sys.stdout.write(result.stdout)
            raise SystemExit(result.returncode)
    unittest.main(argv=[sys.argv[0], *remaining])
