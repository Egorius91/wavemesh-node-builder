"""Execute the wrapper's lock-parent admission with controlled stat/install.

No recovery command or agent is invoked. The install spy fails if the shared
directory becomes a chmod/chown target, reproducing the original regression.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
GIT_BASH = Path('C:/Program Files/Git/bin/bash.exe')
BASH = str(GIT_BASH) if GIT_BASH.is_file() else shutil.which('bash')


@unittest.skipUnless(BASH, 'bash required for wrapper admission checks')
class RecoveryLockParentTests(unittest.TestCase):
    def exercise(self, mode='755', owner='0', directory=True, lock_owner='0', lock_mode='600', lock_links='1', existing_lock=False):
        script = (ROOT/'agent/recover.sh').read_text(encoding='utf-8')
        # Include both the old install instruction and the replacement guards.
        start = script.index('install -d -o root -g root -m 0700 "$(dirname') if 'install -d -o root -g root -m 0700 "$(dirname' in script else script.index('lock_parent=')
        end = script.index('flock -n 9')
        block = script[start:end]
        harness = '''set -Eeuo pipefail
fail() { printf 'FAIL=%s\\n' "$1"; exit 2; }
stat() {
 if [[ "$3" == "$LOCK_FILE" ]]; then
  case "$2" in '%u') printf '%s\\n' "$TEST_LOCK_OWNER";; '%a') printf '%s\\n' "$TEST_LOCK_MODE";; '%h') printf '%s\\n' "$TEST_LOCK_LINKS";; *) exit 9;; esac
 else
  case "$2" in '%u') printf '%s\\n' "$TEST_OWNER";; '%a') printf '%s\\n' "$TEST_MODE";; *) exit 9;; esac
 fi
}
install() {
 for argument in "$@"; do
  if [[ "$argument" == "$(dirname "$LOCK_FILE")" ]]; then printf 'SHARED_PARENT_MUTATED\\n'; exit 8; fi
 done
 printf 'PRIVATE_BACKUP_INSTALL\\n'
}
'''
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)/'shared-lock-directory'
            if directory:parent.mkdir()
            else:parent.write_text('not a directory')
            if existing_lock:(parent/'recovery.lock').write_bytes(b'existing lock sentinel')
            env = {**os.environ, 'LOCK_FILE':str(parent/'recovery.lock'), 'BACKUP_ROOT':str(Path(raw)/'private-backups'),
                   'TEST_MODE':mode, 'TEST_OWNER':owner, 'TEST_LOCK_OWNER':lock_owner,
                   'TEST_LOCK_MODE':lock_mode, 'TEST_LOCK_LINKS':lock_links}
            result = subprocess.run([BASH, '--noprofile', '--norc'], input=harness+block,
                                    text=True, capture_output=True, env=env, timeout=10)
            result.lock_contents=(parent/'recovery.lock').read_bytes() if directory and (parent/'recovery.lock').is_file() else None
            return result

    def test_shared_root_directory_permissions_are_preserved(self):
        for mode in ('755', '700', '1777'):
            with self.subTest(mode=mode):
                result = self.exercise(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'PRIVATE_BACKUP_INSTALL')

    def test_untrusted_owner_stops_before_private_backup_install(self):
        result = self.exercise(owner='1000')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), 'FAIL=recovery_lock_parent_owner')

    def test_writable_without_sticky_or_invalid_mode_stops(self):
        for mode in ('777', '775', '757', '888', '755;echo injected'):
            with self.subTest(mode=mode):
                result = self.exercise(mode)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn('PRIVATE_BACKUP_INSTALL', result.stdout)
                self.assertNotIn('SHARED_PARENT_MUTATED', result.stdout)

    def test_non_directory_stops_before_any_install(self):
        result = self.exercise(directory=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), 'FAIL=recovery_lock_parent_unsafe')

    def test_foreign_writable_and_linked_lock_rejected(self):
        for kwargs, code in (({'lock_owner':'1000'}, 'owner'), ({'lock_mode':'666'}, 'writable'), ({'lock_links':'2'}, 'links')):
            with self.subTest(kwargs=kwargs):
                result = self.exercise(mode='1777', **kwargs)
                self.assertEqual(result.returncode, 2)
                self.assertIn('FAIL=recovery_lock_file_'+code, result.stdout)

    def test_existing_lock_is_opened_without_truncation(self):
        result = self.exercise(mode='1777', existing_lock=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.lock_contents, b'existing lock sentinel')


if __name__ == '__main__':unittest.main()
