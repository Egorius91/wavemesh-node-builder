"""The synthetic diagnostic refuses legacy TLS without dropping peer checks."""
import importlib.util
from pathlib import Path
import ssl
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    'prepared_native_diagnostic', ROOT / 'tests/native/test_prepared_channels_native.py')
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class PreparedDiagnosticTls(unittest.TestCase):
    def test_legacy_provider_floor_is_raised_without_losing_peer_verification(self):
        # A legacy provider is simulated, never configured on a live SSLContext.
        context = mock.Mock(spec=ssl.SSLContext,
            minimum_version=ssl.TLSVersion.MINIMUM_SUPPORTED,
            verify_mode=ssl.CERT_REQUIRED, check_hostname=True)
        with mock.patch.object(fixture.ssl, 'create_default_context', return_value=context) as create:
            actual = fixture.diagnostic_tls_context(Path('synthetic-ca.pem'))
        self.assertIs(actual, context)
        self.assertGreaterEqual(actual.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(actual.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(actual.check_hostname)
        create.assert_called_once_with(cafile='synthetic-ca.pem')

    def test_current_client_defaults_keep_peer_verification_and_tls13_available(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with mock.patch.object(fixture.ssl, 'create_default_context', return_value=context):
            actual = fixture.diagnostic_tls_context('synthetic-ca.pem')
        self.assertGreaterEqual(actual.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(actual.maximum_version, ssl.TLSVersion.MAXIMUM_SUPPORTED)
        self.assertEqual(actual.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(actual.check_hostname)


if __name__ == '__main__':
    unittest.main()
