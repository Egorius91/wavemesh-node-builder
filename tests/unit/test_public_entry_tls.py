"""Target/privacy/composition contracts; real TLS is covered by native tests."""
import copy
import json
from pathlib import Path
import socket
import ssl
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts/lib'))
import public_entry_tls as probe


def config():
    return {'server': {'domain': 'Entry.Example.Net', 'public_ip': '8.8.8.8'},
            'private_fixture': 'do-not-export-native-credentials'}


class RawSocket:
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def settimeout(self, value): pass
    def connect(self, address): self.address = address
    def getpeername(self): return ('8.8.8.8', 443)


class SecureSocket:
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def getpeercert(self, binary_form=False):
        return b'synthetic-der' if binary_form else {'notAfter': 'Nov 17 12:00:00 2030 GMT'}
    def version(self): return 'TLSv1.3'


class Context:
    check_hostname = True
    verify_mode = ssl.CERT_REQUIRED
    def get_ca_certs(self, binary_form=False): return [b'synthetic-root']
    def wrap_socket(self, raw, *, server_hostname):
        assert server_hostname == 'entry.example.net'
        return SecureSocket()


class Targets(unittest.TestCase):
    def test_literal_public_v4_v6_and_dns_case_are_canonical(self):
        self.assertEqual(probe.public_target(config()), ('entry.example.net', '8.8.8.8', 4))
        item = config(); item['server']['public_ip'] = '2001:4860:4860::8888'
        self.assertEqual(probe.public_target(item)[2], 6)

    def test_private_reserved_multicast_mapped_and_scoped_addresses_refuse(self):
        for address in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '192.0.2.1', '224.0.0.1',
                        '::1', 'fc00::1', 'fe80::1%eth0', 'ff02::1', '::ffff:8.8.8.8', 'https://8.8.8.8']:
            with self.subTest(address=address):
                item = config(); item['server']['public_ip'] = address
                with self.assertRaises(probe.ProbeUnavailable): probe.public_target(item)

    def test_domain_is_only_a_hostname_without_url_port_or_private_payload(self):
        for domain in ['localhost', 'https://entry.example.net', 'entry.example.net:443',
                       'entry.example.net/path', ' entry.example.net', 'entry.example.net.',
                       '*.example.net', '8.8.8.8', 'private\nheader.example.net']:
            with self.subTest(domain=domain):
                item = config(); item['server']['domain'] = domain
                with self.assertRaises(probe.ProbeUnavailable): probe.public_target(item)


class Composition(unittest.TestCase):
    def collect(self, *, second_digest='a'*64, context=None, socket_type=RawSocket):
        original = config(); before = copy.deepcopy(original)
        with patch.object(probe, '_configuration', side_effect=[(original, 'a'*64), (original, second_digest)]), \
                patch.object(probe.ssl, 'create_default_context', return_value=context or Context()), \
                patch.object(probe.socket, 'socket', return_value=socket_type()) as created:
            report = probe.collect(Path('synthetic-config.json'))
            self.assertEqual(original, before)
            if report.get('reason') == 'TLS_VERIFICATION_REQUIRED':
                created.assert_not_called()
            else:
                self.assertEqual(created.call_args.args, (socket.AF_INET, socket.SOCK_STREAM))
        return report

    def test_only_tls_evidence_and_frozen_configuration_leave_the_probe(self):
        report = self.collect()
        self.assertEqual(report['status'], 'VERIFIED_TLS_ONLY')
        self.assertEqual(report['scope'], 'public_entry_tls_only')
        self.assertEqual(report['config_sha256'], 'a'*64)
        self.assertEqual(report['port'], 443)
        self.assertNotIn('capability', report); self.assertNotIn('healthy', report)
        self.assertNotIn('do-not-export', json.dumps(report))

    def test_configuration_change_after_handshake_cannot_emit_partial_success(self):
        report = self.collect(second_digest='b'*64)
        self.assertEqual(report['status'], 'NOT_VERIFIED')
        self.assertEqual(report['reason'], 'CONFIGURATION_CHANGED')
        self.assertNotIn('certificate_sha256', report)

    def test_verifier_downgrade_and_private_ssl_errors_are_fixed_refusals(self):
        context = Context(); context.check_hostname = False
        report = self.collect(context=context)
        self.assertEqual(report['reason'], 'TLS_VERIFICATION_REQUIRED')
        class BrokenContext(Context):
            def wrap_socket(self, *_args, **_kwargs):
                raise ssl.SSLCertVerificationError('private-synthetic-certificate-details')
        report = self.collect(context=BrokenContext())
        self.assertEqual(report['reason'], 'TLS_CERTIFICATE_VERIFICATION_FAILED')
        self.assertNotIn('private-synthetic', json.dumps(report))

    def test_unexpected_peer_cannot_be_relabelled_as_configured_ip(self):
        class WrongPeer(RawSocket):
            def getpeername(self): return ('9.9.9.9', 443)
        report = self.collect(socket_type=WrongPeer)
        self.assertEqual(report['reason'], 'PEER_ADDRESS_MISMATCH')


if __name__ == '__main__': unittest.main()
