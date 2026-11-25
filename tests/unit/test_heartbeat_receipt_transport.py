"""Portable transport boundary proof; actual POSIX/TLS remains a native gate."""
import base64
from datetime import datetime, timedelta, timezone
from email.message import Message
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agent'))
import node_mtls_client as client
import node_mtls_runtime as runtime
from heartbeat_observation import HEADER, heartbeat_node_hash


def stamp(value):
    return value.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


class Response:
    status = 204

    def __init__(self, values, body=b''):
        self.headers = Message()
        for value in values:
            self.headers[HEADER] = value
        self.body = body

    def read(self, _amount):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class HeartbeatReceiptTransport(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.sent = stamp(self.now - timedelta(seconds=1))
        self.receipt = dict(schema_version=1, kind='mtls_heartbeat_commit', received_at=stamp(self.now),
            request_sent_at=self.sent, node_sha256=heartbeat_node_hash('tenant-fixture', 'node-fixture'),
            credential_sha256='f'*64, certificate_fingerprint_sha256='a'*64,
            credential_not_before=stamp(self.now-timedelta(hours=1)),
            credential_expires_at=stamp(self.now+timedelta(hours=1)),
            credential_acknowledged_at=stamp(self.now-timedelta(minutes=1)),
            api_producer=dict(artifact_sha256='b'*64, instance_sha256='c'*64),
            ingress_producer=dict(artifact_sha256='d'*64, instance_sha256='e'*64))
        self.path = 'internal/v1/nodes/node-fixture/heartbeat'
        self.active = object()
        self.state = Mock()
        self.state.active_identity.return_value = self.active
        self.ssl = patch.object(client, 'build_mtls_ssl_context', return_value=object())
        self.fp = patch.object(client, '_observation_certificate_fingerprint', return_value='a'*64)
        self.ssl.start()
        self.fingerprint = self.fp.start()
        self.addCleanup(self.ssl.stop)
        self.addCleanup(self.fp.stop)

    def encoded(self, changes=None):
        raw = json.dumps({**self.receipt, **(changes or {})}, separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()

    def transport(self, opener, mode='mtls'):
        return client.NodeMtlsTransport(client.MtlsClientConfig(api_base='https://api.example.invalid',
            node_id='node-fixture', tenant_id='tenant-fixture', environment='staging', auth_mode=mode,
            bearer_token=None if mode == 'mtls' else 'wvn_'+'x'*40), self.state, opener)

    def test_existing_request_owner_emits_only_validated_actual_receipt(self):
        opener = Mock(return_value=Response([self.encoded()]))
        callback = Mock()
        result = self.transport(opener).api_json('POST', self.path, {'sent_at':self.sent}, (204,), heartbeat_receipt=callback)
        self.assertEqual(result, {})
        callback.assert_called_once_with(self.receipt)
        opener.assert_called_once()
        self.state.active_identity.assert_called_once()
        self.fingerprint.assert_called_once_with(self.active)
        self.assertIsNone(opener.call_args.args[0].get_header('Authorization'))

    def test_default_disabled_does_not_read_certificate_or_response_headers(self):
        response = Response([self.encoded()])
        del response.headers
        opener = Mock(return_value=response)
        self.assertEqual(self.transport(opener).api_json('POST', self.path, {'sent_at':self.sent}, (204,)), {})
        self.fingerprint.assert_not_called()
        opener.assert_called_once()

    def test_callback_failure_cannot_retry_or_fail_committed_heartbeat(self):
        opener = Mock(return_value=Response([self.encoded()]))
        callback = Mock(side_effect=OSError('DO_NOT_EXPORT_PRIVATE_TEST_SENTINEL'))
        self.assertEqual(self.transport(opener).api_json('POST', self.path, {'sent_at':self.sent}, (204,), heartbeat_receipt=callback), {})
        callback.assert_called_once()
        opener.assert_called_once()

    def test_lost_reply_is_not_an_observation_and_never_redispatched(self):
        opener = Mock(side_effect=URLError('synthetic lost reply'))
        callback = Mock()
        with self.assertRaises(client.MtlsApiError):
            self.transport(opener).api_json('POST', self.path, {'sent_at':self.sent}, (204,), heartbeat_receipt=callback)
        callback.assert_not_called()
        opener.assert_called_once()

    def test_duplicate_invalid_foreign_or_nonempty_response_never_emits(self):
        for response in [Response([]), Response([self.encoded(),self.encoded()]), Response(['invalid']),
                Response([self.encoded({'certificate_fingerprint_sha256':'b'*64})]),
                Response([self.encoded({'request_sent_at':stamp(self.now)})]),
                Response([self.encoded()],b'{}')]:
            with self.subTest(response=response):
                callback = Mock()
                opener = Mock(return_value=response)
                self.assertEqual(self.transport(opener).api_json('POST', self.path, {'sent_at':self.sent}, (204,), heartbeat_receipt=callback), {})
                callback.assert_not_called()
                opener.assert_called_once()

    def test_other_authentication_method_or_route_does_not_observe(self):
        for mode,method,path in [('bearer','POST',self.path),('bootstrap-mtls','POST',self.path),
                ('mtls','GET',self.path),('mtls','POST',self.path+'-other')]:
            with self.subTest(mode=mode,method=method,path=path):
                callback = Mock()
                opener = Mock(return_value=Response([self.encoded()]))
                self.transport(opener,mode).api_json(method,path,{'sent_at':self.sent},(204,),heartbeat_receipt=callback)
                callback.assert_not_called()
                opener.assert_called_once()
        self.fingerprint.assert_not_called()

    def test_certificate_metadata_failure_is_a_gap_without_transport_change(self):
        self.fingerprint.side_effect = OSError('private certificate read unavailable')
        callback = Mock()
        opener = Mock(return_value=Response([self.encoded()]))
        self.assertEqual(self.transport(opener).api_json('POST',self.path,{'sent_at':self.sent},(204,),heartbeat_receipt=callback),{})
        callback.assert_not_called()
        opener.assert_called_once()

    def test_existing_runtime_forwards_optional_callback_without_another_transport(self):
        owner = object.__new__(runtime.NodeMtlsRuntime)
        owner.config = SimpleNamespace(mode='shadow',node_id='node-fixture')
        owner.state = self.state
        owner._expected_identity_uri = Mock(return_value='synthetic expected URI')
        transport = Mock()
        transport.api_json.return_value = {}
        owner._mtls_transport = Mock(return_value=transport)
        owner.status = Mock(return_value=SimpleNamespace(state=runtime.MtlsAgentState.SHADOW_ACTIVE))
        callback = Mock()
        with patch.object(runtime,'parse_certificate_expiry',return_value=self.now+timedelta(hours=1)):
            self.assertEqual(owner.api_json('POST',self.path,{'sent_at':self.sent},(204,),heartbeat_receipt=callback),{})
        owner._mtls_transport.assert_called_once()
        transport.api_json.assert_called_once_with('POST',self.path,{'sent_at':self.sent},expected=(204,),headers=None,heartbeat_receipt=callback)


if __name__ == '__main__':
    unittest.main(verbosity=2)
