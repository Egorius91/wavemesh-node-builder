"""Actual TLS on two synthetic guest-local IPs; no Xray/Node/live acceptance.

Run as root in a fresh network namespace of an admitted disposable Linux host.
The namespace has only loopback; its synthetic CA is trusted only by these tests.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts/lib'))
import public_entry_tls as probe

V4 = '11.77.0.1'
V6 = '2001:4860:ffff::1234'
DOMAIN = 'entry.example.net'


def run(*args):
    subprocess.run(args, check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@unittest.skipUnless(sys.platform == 'linux' and hasattr(os, 'geteuid') and os.geteuid() == 0,
                     'Native Linux root fixture required; not a portable TLS proof')
class NativeTLS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.readlink('/proc/self/ns/net') == os.readlink('/proc/1/ns/net'):
            raise AssertionError('PRIVATE_NETWORK_NAMESPACE_REQUIRED')
        interfaces = json.loads(subprocess.check_output(['ip', '-j', 'link', 'show']))
        if [interface['ifname'] for interface in interfaces] != ['lo']:
            raise AssertionError('LOOPBACK_ONLY_NAMESPACE_REQUIRED')
        run('ip', 'link', 'set', 'lo', 'up')
        cls.stack = contextlib.ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.root = Path(cls.stack.enter_context(tempfile.TemporaryDirectory(prefix='wm-public-entry-tls-native-')))
        for address in (V4, V6):
            family = '-4' if ipaddress.ip_address(address).version == 4 else '-6'
            present = subprocess.check_output(['ip', family, '-j', 'address', 'show', 'dev', 'lo'])
            if any(item.get('local') == address for interface in json.loads(present)
                   for item in interface.get('addr_info', [])):
                raise AssertionError('SYNTHETIC_ADDRESS_ALREADY_PRESENT')
            prefix = address + ('/32' if family == '-4' else '/128')
            run('ip', family, 'address', 'add', prefix, 'dev', 'lo')
            cls.stack.callback(run, 'ip', family, 'address', 'del', prefix, 'dev', 'lo')
        cls.ca = cls.root/'ca.pem'
        run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=synthetic-public-entry-test-ca', '-keyout', str(cls.root/'ca.key'), '-out', str(cls.ca))
        # Reuse the existing expired-identity fixture's explicit validity dates.
        # New OpenSSL rejects x509 -days -1. Keep the CA older than either leaf.
        (cls.root/'index').touch(); (cls.root/'serial').write_text('1000\n')
        (cls.root/'issued').mkdir()
        ca_config = cls.root/'ca.cnf'
        ca_config.write_text('\n'.join([
            '[ca]', 'default_ca=fixture', '[fixture]', 'default_md=sha256', 'default_days=1',
            'database='+str(cls.root/'index'), 'serial='+str(cls.root/'serial'),
            'new_certs_dir='+str(cls.root/'issued'), 'private_key='+str(cls.root/'ca.key'),
            'certificate='+str(cls.ca), 'policy=identity', 'unique_subject=no',
            '[identity]', 'commonName=supplied',
            '[ca_ext]', 'basicConstraints=critical,CA:TRUE', 'keyUsage=critical,keyCertSign,cRLSign',
            '[leaf_ext]', 'basicConstraints=critical,CA:FALSE',
            'keyUsage=digitalSignature,keyEncipherment', 'extendedKeyUsage=serverAuth',
            'subjectAltName=DNS:'+DOMAIN, '']))
        run('openssl', 'req', '-new', '-key', str(cls.root/'ca.key'),
            '-subj', '/CN=synthetic-public-entry-test-ca', '-out', str(cls.root/'ca.csr'))
        now = datetime.now(timezone.utc)
        date = lambda days: (now+timedelta(days=days)).strftime('%y%m%d%H%M%SZ')
        historical_ca = cls.root/'historical-ca.pem'
        run('openssl', 'ca', '-batch', '-notext', '-selfsign', '-config', str(ca_config),
            '-in', str(cls.root/'ca.csr'), '-extensions', 'ca_ext',
            '-startdate', date(-3), '-enddate', date(2), '-out', str(historical_ca))
        cls.ca.write_bytes(historical_ca.read_bytes())
        run('openssl', 'req', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN='+DOMAIN,
            '-keyout', str(cls.root/'leaf.key'), '-out', str(cls.root/'leaf.csr'))
        (cls.root/'leaf.ext').write_text('subjectAltName=DNS:'+DOMAIN+'\n'
                                       'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n'
                                       'extendedKeyUsage=serverAuth\n')
        run('openssl', 'x509', '-req', '-in', str(cls.root/'leaf.csr'), '-CA', str(cls.ca),
            '-CAkey', str(cls.root/'ca.key'), '-set_serial', '2', '-days', '1',
            '-extfile', str(cls.root/'leaf.ext'), '-out', str(cls.root/'leaf.pem'))
        run('openssl', 'ca', '-batch', '-notext', '-config', str(ca_config),
            '-in', str(cls.root/'leaf.csr'), '-extensions', 'leaf_ext',
            '-startdate', date(-2), '-enddate', date(-1), '-out', str(cls.root/'expired.pem'))
        cls.empty_roots = cls.root/'empty-ca-directory'; cls.empty_roots.mkdir()
        cls.ca_environment = {'SSL_CERT_FILE': str(cls.ca), 'SSL_CERT_DIR': str(cls.empty_roots)}

    def configuration(self, address=V4, domain=DOMAIN):
        path = self.root/'config.json'
        path.write_text(json.dumps({'server': {'domain': domain, 'public_ip': address},
                                    'private_fixture': 'never-export-this-credential'}))
        path.chmod(0o600)
        return path

    @contextlib.contextmanager
    def server(self, *, address=V4, certificate='leaf.pem'):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.root/certificate, self.root/'leaf.key')
        listener = socket.socket(socket.AF_INET if address == V4 else socket.AF_INET6, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((address, 443)); listener.listen(1); listener.settimeout(5)
        errors = []
        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    try:
                        with context.wrap_socket(connection, server_side=True): pass
                    except ssl.SSLError: pass  # Expected for certificate negatives.
            except Exception as error:
                errors.append(type(error).__name__)
        thread = threading.Thread(target=serve, daemon=True); thread.start()
        try:
            yield
        finally:
            thread.join(timeout=7); listener.close()
            self.assertFalse(thread.is_alive(), 'SYNTHETIC_TLS_THREAD_REMAINS')
            self.assertEqual(errors, [], 'SYNTHETIC_TLS_LISTENER_FAILED')

    def test_actual_cli_validates_chain_name_and_fingerprint_without_http(self):
        config = self.configuration()
        with self.server():
            completed = subprocess.run([sys.executable, '-B', str(Path(probe.__file__)), '--config', str(config)],
                env={**os.environ, **self.ca_environment}, capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(completed.stderr, '')
        report = json.loads(completed.stdout)
        self.assertEqual(report['status'], 'VERIFIED_TLS_ONLY')
        der = ssl.PEM_cert_to_DER_cert((self.root/'leaf.pem').read_text())
        self.assertEqual(report['certificate_sha256'], hashlib.sha256(der).hexdigest())
        self.assertEqual(report['config_sha256'], hashlib.sha256(config.read_bytes()).hexdigest())
        self.assertGreater(report['loaded_trust_root_count'], 0)
        self.assertNotIn('never-export', completed.stdout)
        self.assertNotIn('capability', report)

    def test_actual_ipv6_tls_uses_same_strict_verifier(self):
        config = self.configuration(address=V6)
        with patch.dict(os.environ, self.ca_environment), self.server(address=V6):
            report = probe.collect(config)
        self.assertEqual(report['status'], 'VERIFIED_TLS_ONLY')
        self.assertEqual(report['address_family'], 6)

    def test_actual_wrong_hostname_is_rejected_without_certificate_output(self):
        config = self.configuration(domain='wrong.example.net')
        with patch.dict(os.environ, self.ca_environment), self.server():
            report = probe.collect(config)
        self.assertEqual(report['reason'], 'TLS_CERTIFICATE_VERIFICATION_FAILED')
        self.assertNotIn('certificate_sha256', report)

    def test_actual_untrusted_and_expired_chains_are_rejected(self):
        config = self.configuration()
        with patch.dict(os.environ, {'SSL_CERT_FILE': str(self.root/'absent-ca'), 'SSL_CERT_DIR': str(self.empty_roots)}), self.server():
            untrusted = probe.collect(config)
        self.assertEqual(untrusted['reason'], 'TLS_CERTIFICATE_VERIFICATION_FAILED')
        with patch.dict(os.environ, self.ca_environment), self.server(certificate='expired.pem'):
            expired = probe.collect(config)
        self.assertEqual(expired['reason'], 'TLS_CERTIFICATE_VERIFICATION_FAILED')

    def test_native_config_custody_and_ambiguous_json_refuse_before_connect(self):
        config = self.configuration()
        for mode in (0o644, 0o666):
            config.chmod(mode)
            with patch.object(probe.socket, 'socket') as transport:
                self.assertEqual(probe.collect(config)['reason'], 'CONFIGURATION_UNAVAILABLE')
                transport.assert_not_called()
        config.chmod(0o600)
        hard = self.root/'config-hard.json'; os.link(config, hard)
        self.assertEqual(probe.collect(config)['reason'], 'CONFIGURATION_UNAVAILABLE')
        hard.unlink()
        fifo = self.root/'config-fifo.json'; os.mkfifo(fifo, 0o600)
        self.assertEqual(probe.collect(fifo)['reason'], 'CONFIGURATION_UNAVAILABLE')
        os.chown(config, 65534, 65534)
        self.assertEqual(probe.collect(config)['reason'], 'CONFIGURATION_UNAVAILABLE')
        os.chown(config, 0, 0)
        link = self.root/'config-link.json'; link.symlink_to(config)
        with patch.object(probe.socket, 'socket') as transport:
            self.assertEqual(probe.collect(link)['reason'], 'CONFIGURATION_UNAVAILABLE')
            transport.assert_not_called()
        config.write_text('{"server":{},"server":{}}'); config.chmod(0o600)
        self.assertEqual(probe.collect(config)['reason'], 'CONFIGURATION_UNAVAILABLE')


if __name__ == '__main__':
    if '--require-native' in sys.argv:
        sys.argv.remove('--require-native')
        if sys.platform != 'linux' or not hasattr(os, 'geteuid') or os.geteuid() != 0:
            raise SystemExit(77)
        if not shutil.which('openssl') or not shutil.which('ip'):
            raise SystemExit(77)
        if os.readlink('/proc/self/ns/net') == os.readlink('/proc/1/ns/net'):
            raise SystemExit(77)
    unittest.main()
