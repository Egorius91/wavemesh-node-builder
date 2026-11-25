"""Real loopback mutual TLS and receipt transport, using disposable identities.

The fixed identity provider is a test fixture, not NodeMtlsState admission.
No issuer, external network, installed identity, service or credentials are used.
"""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'agent'))
from heartbeat_observation import HEADER, heartbeat_node_hash
from node_mtls_client import MtlsClientConfig, NodeMtlsTransport
from node_mtls_state import ActiveIdentity


def stamp(value):
    return value.isoformat(timespec='milliseconds').replace('+00:00','Z')


class LoopbackHeartbeatReceiptTls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != 'posix' or os.geteuid() != 0:
            raise RuntimeError('NATIVE_LINUX_ROOT_REQUIRED_NOT_A_SKIP')
        cls.parent = Path(os.environ.get('PR8_NATIVE_TMPDIR','/var/tmp'))
        if not (cls.parent == Path('/var/tmp') or re.fullmatch(
                '/var/tmp/wm-pr8-managed-qualification-[a-f0-9]{12}/native-tmp',str(cls.parent))):
            raise RuntimeError('FIXED_DISPOSABLE_TLS_PARENT_REQUIRED')
        if cls.parent.resolve() != cls.parent:
            raise RuntimeError('FIXED_DISPOSABLE_TLS_PARENT_REQUIRED')
        cls.directory = Path(tempfile.mkdtemp(prefix='wm-pr8-tls-', dir=cls.parent))
        cls.directory.chmod(0o700)
        cls.openssl('req','-new','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256',
            '-nodes','-days','1','-subj','/CN=PR8 disposable CA',
            '-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign',
            '-keyout','ca.key','-out','ca.crt')
        for name,usage,san,serial in [('server','serverAuth','IP:127.0.0.1','2'),
                ('client','clientAuth','URI:spiffe://wavevpn/staging/tenant/tenant-fixture/node/node-fixture','3')]:
            cls.openssl('req','-new','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256',
                '-nodes','-subj','/CN=PR8 disposable '+name,'-keyout',name+'.key','-out',name+'.csr')
            (cls.directory/(name+'.ext')).write_text('basicConstraints=critical,CA:FALSE\n'
                'keyUsage=critical,digitalSignature\nextendedKeyUsage='+usage+'\nsubjectAltName='+san+'\n')
            cls.openssl('x509','-req','-in',name+'.csr','-CA','ca.crt','-CAkey','ca.key',
                '-set_serial',serial,'-days','1','-extfile',name+'.ext','-out',name+'.crt')
        (cls.directory/'chain.pem').write_bytes((cls.directory/'client.crt').read_bytes()+(cls.directory/'ca.crt').read_bytes())
        for path in cls.directory.iterdir():
            path.chmod(0o600)
        cls.client_fp = hashlib.sha256(ssl.PEM_cert_to_DER_cert((cls.directory/'client.crt').read_text())).hexdigest()

    @classmethod
    def openssl(cls, *args):
        process = subprocess.Popen(['openssl',*args], cwd=cls.directory,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            process.communicate(timeout=20)
            if process.returncode != 0:
                raise AssertionError('SYNTHETIC_TLS_FIXTURE_OPENSSL_FAILED')
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=3)
            process.stderr.close()

    @classmethod
    def tearDownClass(cls):
        resolved = cls.directory.resolve()
        if resolved.parent != cls.parent or not resolved.name.startswith('wm-pr8-tls-'):
            raise AssertionError('FIXED_DISPOSABLE_TLS_NAMESPACE_REQUIRED')
        shutil.rmtree(resolved)

    def setUp(self):
        self.requests = []
        self.mode = 'valid'
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):
                pass
            def do_POST(self):
                peer = hashlib.sha256(self.connection.getpeercert(binary_form=True)).hexdigest()
                body = self.rfile.read(int(self.headers['Content-Length']))
                payload = json.loads(body)
                owner.requests.append((self.path,peer,self.headers.get('Authorization')))
                if owner.mode == 'lost-reply':
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                now = datetime.now(timezone.utc)
                receipt = dict(schema_version=1,kind='mtls_heartbeat_commit',received_at=stamp(now),
                    request_sent_at=payload['sent_at'],node_sha256=heartbeat_node_hash('tenant-fixture','node-fixture'),
                    credential_sha256='a'*64,certificate_fingerprint_sha256=peer,
                    credential_not_before=stamp(now-timedelta(hours=1)),credential_expires_at=stamp(now+timedelta(hours=1)),
                    credential_acknowledged_at=stamp(now-timedelta(minutes=1)),
                    api_producer=dict(artifact_sha256='b'*64,instance_sha256='c'*64),
                    ingress_producer=dict(artifact_sha256='d'*64,instance_sha256='e'*64))
                if owner.mode == 'foreign-fingerprint':
                    receipt['certificate_fingerprint_sha256'] = '0'*64
                encoded = base64.urlsafe_b64encode(json.dumps(receipt,separators=(',',':')).encode()).rstrip(b'=').decode()
                self.send_response(204)
                self.send_header(HEADER,encoded)
                if owner.mode == 'duplicate': self.send_header(HEADER,encoded)
                self.send_header('Content-Length','0'); self.end_headers()
        self.server = HTTPServer(('127.0.0.1',0), Handler)
        self.server.timeout = 5
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(self.directory/'server.crt'),str(self.directory/'server.key'))
        context.load_verify_locations(cafile=str(self.directory/'ca.crt'))
        context.verify_mode = ssl.CERT_REQUIRED
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=lambda:self.server.serve_forever(poll_interval=0.05),daemon=False)
        self.thread.start()
        active = ActiveIdentity('1'*24,self.directory,self.directory/'client.key',self.directory/'client.crt',
            self.directory/'chain.pem',self.directory/'ca.crt',self.directory/'unused-metadata.json')
        class FixedIdentity:
            def active_identity(self, expected_identity_uri=None):
                owner.assertEqual(expected_identity_uri,'spiffe://wavevpn/staging/tenant/tenant-fixture/node/node-fixture')
                return active
        self.transport = NodeMtlsTransport(MtlsClientConfig(api_base='https://127.0.0.1:'+str(self.server.server_port),
            tenant_id='tenant-fixture',node_id='node-fixture',environment='staging',auth_mode='mtls',
            request_timeout_seconds=5,server_ca_file=self.directory/'ca.crt'),state=FixedIdentity())

    def tearDown(self):
        self.server.shutdown(); self.thread.join(timeout=3); self.server.server_close()
        self.assertFalse(self.thread.is_alive())

    def heartbeat(self, callback):
        return self.transport.api_json('POST','internal/v1/nodes/node-fixture/heartbeat',
            {'sent_at':stamp(datetime.now(timezone.utc))},(204,),heartbeat_receipt=callback)

    def test_real_verified_client_certificate_correlates_one_empty_204(self):
        receipts = []
        self.assertEqual(self.heartbeat(receipts.append),{})
        self.assertEqual(len(receipts),1)
        self.assertEqual(receipts[0]['certificate_fingerprint_sha256'],self.client_fp)
        self.assertEqual(self.requests,[('/internal/v1/nodes/node-fixture/heartbeat',self.client_fp,None)])

    def test_real_duplicate_or_foreign_headers_never_qualify_or_retry(self):
        for mode in ('duplicate','foreign-fingerprint'):
            self.mode = mode
            receipts = []; before = len(self.requests)
            self.assertEqual(self.heartbeat(receipts.append),{})
            self.assertEqual(receipts,[])
            self.assertEqual(len(self.requests),before+1)

    def test_lost_reply_after_server_receives_one_request_is_never_redispatched(self):
        self.mode = 'lost-reply'
        receipts = []
        with self.assertRaises(Exception):
            self.heartbeat(receipts.append)
        self.assertEqual(len(self.requests),1)
        self.assertEqual(receipts,[])

    def test_optional_consumer_fault_does_not_fail_committed_heartbeat(self):
        def broken(_receipt):
            raise OSError('synthetic optional consumer failure')
        self.assertEqual(self.heartbeat(broken),{})
        self.assertEqual(len(self.requests),1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
