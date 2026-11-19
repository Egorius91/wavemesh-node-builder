"""Every published profile must use the configured public Entry authority."""
import base64
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/lib"))
from native_subscription import validate_content


DOMAIN = "entry.example.com"
PROFILE = "vless://synthetic@entry.example.com:443?security=tls&type=xhttp#Entry"


class NativeSubscriptionAuthority(unittest.TestCase):
    def test_raw_and_base64_multiple_valid_profiles_keep_existing_count(self):
        raw = PROFILE + "\n" + PROFILE.replace("#Entry", "#Second") + "\n"
        for body in (raw, base64.b64encode(raw.encode()).decode()):
            with self.subTest(encoded=body != raw):
                self.assertEqual(validate_content(body, DOMAIN, [], 2), {"profiles": 2})

    def test_each_profile_requires_the_expected_host(self):
        foreign = PROFILE.replace(DOMAIN, "foreign.example.com")
        for body in (PROFILE + "\n" + foreign, foreign + "\n" + PROFILE):
            with self.subTest(foreign_first=body.startswith(foreign)):
                with self.assertRaises(ValueError):
                    validate_content(body, DOMAIN, [], 2)

    def test_authority_in_query_or_fragment_cannot_approve_a_foreign_host(self):
        foreign = PROFILE.replace(DOMAIN, "foreign.example.com")
        for body in (foreign + "-@entry.example.com:443", foreign.replace("#Entry", "&sni=@entry.example.com:443")):
            with self.assertRaises(ValueError):
                validate_content(body, DOMAIN, [])

    def test_port_and_malformed_authority_are_checked_per_profile(self):
        for address in ("entry.example.com:444", "entry.example.com", "entry.example.com:bad",
                        "entry.example.com:65536", "[invalid]:443", "entry.example.com.evil:443"):
            with self.subTest(address=address):
                bad = PROFILE.replace("entry.example.com:443", address)
                with self.assertRaises(ValueError):
                    validate_content(PROFILE + "\n" + bad, DOMAIN, [])

    def test_empty_identity_password_and_control_bytes_are_refused(self):
        for bad in (PROFILE.replace("synthetic@", "@"), PROFILE.replace("synthetic@", "synthetic:password@"),
                    PROFILE.replace("entry.example.com", "entry.\texample.com"), PROFILE + "\x7f"):
            with self.assertRaises(ValueError):
                validate_content(PROFILE + "\n" + bad, DOMAIN, [])

    def test_dns_case_and_fragment_names_remain_compatible(self):
        self.assertEqual(validate_content(PROFILE.replace(DOMAIN, DOMAIN.upper()), DOMAIN, []), {"profiles": 1})
        self.assertEqual(validate_content(PROFILE + "-%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0", DOMAIN, []), {"profiles": 1})

    def test_existing_hidden_internal_and_count_guards_still_apply(self):
        for forbidden, body, count in (([], PROFILE + "--!", 1), (["synthetic"], PROFILE, 1),
                                       ([], PROFILE, 2), ([], PROFILE.replace(DOMAIN, "127.0.0.1"), 1)):
            with self.assertRaises(ValueError):
                validate_content(body, DOMAIN, forbidden, count)


if __name__ == "__main__":
    unittest.main()
