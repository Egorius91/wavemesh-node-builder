"""Only the eight independently triaged immutable false positives are ignored."""
from pathlib import Path
import importlib.util
import unittest

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = (
    '3ca26469524683100497adb634fac032031cf2b9:tests/native/test_xui_owner_central_postgres.py:generic-api-key:151',
    '40100ebda67fe4b3778b3015fe985203016e350d:operations/xui_owner_central_schema.json:generic-api-key:212',
    '40100ebda67fe4b3778b3015fe985203016e350d:operations/xui_owner_central_schema.json:generic-api-key:217',
    '40100ebda67fe4b3778b3015fe985203016e350d:operations/xui_owner_central_schema.json:generic-api-key:219',
    '40100ebda67fe4b3778b3015fe985203016e350d:operations/xui_owner_central_schema.json:generic-api-key:223',
    '40100ebda67fe4b3778b3015fe985203016e350d:operations/xui_owner_central_schema.json:generic-api-key:227',
    '40100ebda67fe4b3778b3015fe985203016e350d:operations/xui_owner_central_schema.json:generic-api-key:248',
    '40100ebda67fe4b3778b3015fe985203016e350d:operations/xui_owner_central_schema.json:generic-api-key:250',
)


def exact_exceptions(text):
    if tuple(text.splitlines()) != EXPECTED:
        raise AssertionError('GITLEAKS_ONLY_EIGHT_REVIEWED_IMMUTABLE_FINGERPRINTS')


class GitleaksExactFingerprints(unittest.TestCase):
    def test_exact_eight_fingerprints_only(self):
        exact_exceptions((ROOT/'.gitleaksignore').read_text())

    def test_broadened_extra_or_retargeted_exception_refused(self):
        good='\n'.join(EXPECTED)+'\n'
        for changed in (good+'operations/*\n',good+'generic-api-key\n',good.replace('40100ebda67fe4b3778b3015fe985203016e350d:', '*:',1),
                        good.replace(':212',':213',1),good+'0'*40+':other.py:generic-api-key:1\n',
                        '\n'.join(EXPECTED[:-1])+'\n'):
            with self.subTest(changed=changed[:40]),self.assertRaises(AssertionError): exact_exceptions(changed)

    def test_full_history_scan_remains_mandatory(self):
        workflow=(ROOT/'.github/workflows/gitleaks.yml').read_text()
        self.assertIn('fetch-depth: 0',workflow)
        self.assertIn('run: gitleaks git --redact --verbose .',workflow)
        jobs=workflow.split('\njobs:\n',1)[1]
        self.assertNotIn('if:',jobs);self.assertNotIn('continue-on-error',jobs)
        for shortcut in ('--log-opts','--no-history','--ignore-path','--gitleaks-ignore-path'):
            self.assertNotIn(shortcut,jobs)

    def test_clock_placeholder_is_not_a_credential_or_sql_literal(self):
        path=ROOT/'tests/native/test_xui_owner_central_postgres.py'
        spec=importlib.util.spec_from_file_location('synthetic_clock_fixture',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        marker='_'.join(('FIXTURE','NOW','MINUS','MINUTE'))
        sql=module.insert('NodeCredential',certificateDeliveryExpiresAt=marker)
        self.assertIn("now()-interval '1 minute'",sql)
        self.assertNotIn(marker,sql)


if __name__ == '__main__':
    unittest.main()
