#!/usr/bin/env python3
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
RECOVERY_WRAPPER = ROOT / "agent" / "recover.sh"


class RecoveryWrapperSecretCleanupTests(unittest.TestCase):
    def test_success_path_destroys_backup_token_after_mtls_acceptance(self) -> None:
        script = RECOVERY_WRAPPER.read_text(encoding="utf-8")

        mtls_acceptance = script.index('[[ "$mtls_ready" == yes ]]')
        backup_delete = script.index('rm -f -- "$backup_dir/recovery.token.before"')
        success_marker = script.index("NODE_SCOPED_CREDENTIAL_RECOVERY=PASS")

        self.assertGreater(backup_delete, mtls_acceptance)
        self.assertLess(backup_delete, success_marker)
        self.assertIn("node_recovery_backup_token_destroyed=yes", script)
        self.assertIn("SHA256SUMS.next", script)


if __name__ == "__main__":
    unittest.main()
