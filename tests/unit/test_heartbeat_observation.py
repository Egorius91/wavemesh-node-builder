import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agent'))
from heartbeat_observation import bind_ingress_receipt, heartbeat_credential_hash, heartbeat_node_hash, parse_ingress_receipt


def receipt():
    return dict(schema_version=1, kind='mtls_heartbeat_commit', received_at='2026-11-21T13:00:00.000Z',
        request_sent_at='2026-11-21T12:59:59.123Z', node_sha256=heartbeat_node_hash('tenant-fixture', 'node-fixture'),
        credential_sha256=heartbeat_credential_hash('tenant-fixture', 'node-fixture', 'credential-fixture'),
        certificate_fingerprint_sha256='a'*64, credential_not_before='2026-11-20T00:00:00.000Z',
        credential_expires_at='2026-11-22T00:00:00.000Z', credential_acknowledged_at='2026-11-20T01:00:00.000Z',
        api_producer=dict(artifact_sha256='b'*64, instance_sha256='c'*64),
        ingress_producer=dict(artifact_sha256='d'*64, instance_sha256='e'*64))


def encoded(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


class HeartbeatObservation(unittest.TestCase):
    def test_complete_ingress_receipt_and_real_request_binding(self):
        value = receipt()
        self.assertEqual(parse_ingress_receipt(encoded(value)), value)
        self.assertEqual(bind_ingress_receipt(encoded(value), tenant_id='tenant-fixture', node_id='node-fixture',
            request_sent_at='2026-11-21T12:59:59.123456Z', certificate_fingerprint_sha256='a'*64,
            now=datetime(2026,11,21,13,tzinfo=timezone.utc)), value)

    def test_duplicate_keys_whitespace_padding_and_invalid_utf8_are_refused(self):
        raw = json.dumps(receipt(), separators=(',', ':'))
        malformed = [raw.replace('"schema_version":1', '"schema_version":1,"schema_version":1'), ' '+raw]
        for item in malformed:
            self.assertIsNone(parse_ingress_receipt(base64.urlsafe_b64encode(item.encode()).rstrip(b'=').decode()))
        self.assertIsNone(parse_ingress_receipt(encoded(receipt())+'='))
        self.assertIsNone(parse_ingress_receipt(base64.urlsafe_b64encode(b'\xff').rstrip(b'=').decode()))

    def test_unfiltered_fields_or_incomplete_producers_never_pass(self):
        for changes in [{'token':'DO_NOT_EXPORT_TEST_SENTINEL'}, {'schema_version': True},
                        {'api_producer': {'artifact_sha256':'b'*64}}, {'credential_sha256':'A'*64}]:
            with self.subTest(changes=changes):
                self.assertIsNone(parse_ingress_receipt(encoded({**receipt(), **changes})))
        value = receipt()
        del value['ingress_producer']
        self.assertIsNone(parse_ingress_receipt(encoded(value)))

    def test_invalid_or_noncanonical_time_and_unacknowledged_or_expired_scope_refused(self):
        for changes in [{'received_at':'2026-11-31T13:00:00.000Z'}, {'received_at':'2026-11-21T13:00:00Z'},
                        {'credential_not_before':'2026-11-22T00:00:00.000Z'},
                        {'credential_acknowledged_at':'2026-11-22T00:00:00.000Z'},
                        {'credential_expires_at':'2026-11-21T13:00:00.000Z'}]:
            with self.subTest(changes=changes):
                self.assertIsNone(parse_ingress_receipt(encoded({**receipt(), **changes})))

    def test_foreign_node_certificate_request_or_stale_response_is_a_gap(self):
        options = dict(tenant_id='tenant-fixture',node_id='node-fixture',request_sent_at='2026-11-21T12:59:59.123Z',
            certificate_fingerprint_sha256='a'*64,now=datetime(2026,11,21,13,tzinfo=timezone.utc))
        for changes in [{'node_id':'foreign-node'}, {'certificate_fingerprint_sha256':'f'*64},
                        {'request_sent_at':'2026-11-21T12:59:59.124Z'},
                        {'now':options['now']+timedelta(seconds=121)}, {'now':options['now']-timedelta(seconds=6)},
                        {'now':datetime(2026,11,21,13)}]:
            with self.subTest(changes=changes):
                self.assertIsNone(bind_ingress_receipt(encoded(receipt()), **{**options, **changes}))

    def test_oversized_or_nonstring_metadata_is_refused(self):
        for value in [None, {}, [], '', 'A'*2049]:
            self.assertIsNone(parse_ingress_receipt(value))


if __name__ == '__main__':
    unittest.main(verbosity=2)
