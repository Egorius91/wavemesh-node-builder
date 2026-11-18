"""Private one-send fixtures, synthetic credentials and literal loopback only."""
from contextlib import closing, contextmanager, nullcontext
import copy
import http.server
import json
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/lib'))
import prepared_owner as owner
import prepared_completion as completion
import prepared_execution as execution
import prepared_journal as reader
import transaction_state as journal
from tests.unit.test_prepared_completion import PreparedCompletionWriter


class PreparedOwner(unittest.TestCase):
    def setUp(self):
        self.factory = PreparedCompletionWriter()
        self.factory.setUp()
        self.addCleanup(self.factory.doCleanups)
        self.token = 'synthetic-existing-admin-token-12345678901234567890'

    def seed(self, case, port):
        directory, intent, reply, view, effective = case
        with closing(sqlite3.connect(intent['files']['database'])) as db:
            raw_template = json.dumps(reader.decode(db.execute('SELECT value FROM settings').fetchone()[0]), ensure_ascii=False, indent=3) + '\n'
            db.execute('UPDATE settings SET value=?', (raw_template,))
            db.executemany('INSERT INTO settings(key,value) VALUES (?,?)', [('webListen','127.0.0.1'), ('webPort',str(port)), ('webBasePath','/private/'), ('webCertFile',''), ('webKeyFile','')])
            db.execute('CREATE TABLE api_tokens (token TEXT,enabled INTEGER,scope TEXT,expires_at INTEGER)')
            db.execute('INSERT INTO api_tokens VALUES (?,?,?,?)', (reader.sha(self.token.encode()),1,'admin',0))
            db.commit()
        intent['before']['template'] = reader.sha(raw_template.encode())
        journal.atomic_json(directory/'prepared.apply.pending.json', intent)
        reply['intent_sha256'] = reader.sha((directory/'prepared.apply.pending.json').read_bytes())
        credentials = directory/'synthetic-bearer.json'
        journal.atomic_json(credentials, {'schema_version':1,'bearer_token':self.token})
        return credentials, raw_template

    @contextmanager
    def server(self, reply, mode='reply', code=200, diagnostic='COMMITTED'):
        state = {'requests':[]}
        test = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                state['requests'].append((self.path,self.headers['Authorization'],reader.decode(raw)))
                if mode == 'lost':
                    self.close_connection = True
                    return
                self.send_response(code)
                self.send_header(owner.DIAGNOSTIC,diagnostic)
                self.send_header('Content-Type','application/json')
                if mode == 'duplicate-header':self.send_header(owner.DIAGNOSTIC,diagnostic)
                if mode == 'redirect': self.send_header('Location','http://example.invalid/not-allowed')
                body = json.dumps(reply).encode()
                if mode == 'duplicate-json':body=body.replace(b'"schema_version": 1',b'"schema_version":1,"schema_version":1',1)
                if mode == 'oversized':body=b' '*(reader.LIMIT+1)
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        thread.start()
        try: yield server.server_port,state
        finally:
            server.shutdown();server.server_close();thread.join(5)
            test.assertFalse(thread.is_alive(),'own loopback fixture did not close')

    def apply(self, case, credentials, collector=None, mock_peer=True):
        directory,intent,reply,view,effective=case
        def collect():
            self.assertTrue((directory/'prepared.owner-result.json').is_file(),'collection preceded durable owner reply')
            return view,effective
        # Windows protocol fixtures explicitly stub native peer custody. The
        # actual Linux accepted-socket test is separate, never inferred here.
        peer = mock.patch.object(execution,'verified_panel_socket',return_value=nullcontext()) if mock_peer else nullcontext()
        with peer,mock.patch.object(completion,'_require_node_owner'),mock.patch.object(execution,'collect',side_effect=collector or collect):
            return owner.apply_once(directory,credentials)

    def test_one_real_send_exact_raw_template_reply_before_collect_and_completion(self):
        case=self.factory.make_case();directory,intent,reply,view,effective=case
        with self.server(reply) as (port,state):
            credentials,raw_template=self.seed(case,port)
            database_before=Path(intent['files']['database']).read_bytes()
            with mock.patch.object(owner.socket,'getaddrinfo',side_effect=AssertionError('literal loopback resolved DNS')):
                self.assertEqual(self.apply(case,credentials),'committed')
            self.assertEqual(len(state['requests']),1)
            path,authorization,request=state['requests'][0]
            self.assertEqual(path,'/private/panel/api/xray/executionSelector')
            self.assertEqual(authorization,'Bearer '+self.token)
            self.assertEqual(request['template_before'],raw_template)
            self.assertEqual(request['expected']['effective_config_sha256'],intent['expected']['before_sha256'])
            self.assertEqual(Path(intent['files']['database']).read_bytes(),database_before)
            self.assertTrue((directory/owner.DISPATCH).is_file())
            self.assertEqual(reader.prepared_status(directory),'committed')

    def test_partial_and_noop_replies_are_retained_without_collection_or_replay(self):
        for kind in ('partial','no-op'):
            case=self.factory.make_case(kind);directory,intent,reply,view,effective=case
            reply['outcome']['KnownCommit']=False
            if kind=='partial':reply['outcome']['ConfigPersisted']=False;reply['outcome']['ProcessConfigSHA256']=''
            else:reply['outcome']={**{k:k=='NoOp' for k in reader.OUTCOME},'ProcessConfigSHA256':''}
            with self.server(reply,code=409 if kind=='partial' else 200,diagnostic='OUTCOME_UNPROVEN' if kind=='partial' else 'NO_OP') as (port,state):
                credentials,_=self.seed(case,port)
                self.assertEqual(self.apply(case,credentials,collector=AssertionError('partial reply collected')),'pending')
                self.assertEqual(reader.decode((directory/'prepared.owner-result.json').read_bytes()),reply)
                self.assertEqual(reader.prepared_status(directory),'active')
                with self.assertRaises(owner.OwnerUnavailable):self.apply(case,credentials)
                self.assertEqual(len(state['requests']),1)

    def test_lost_response_redirect_and_unauthorized_never_retry_or_fallback(self):
        for mode,code in (('lost',200),('redirect',302),('unauthorized',401)):
            case=self.factory.make_case(mode);directory,intent,reply,view,effective=case
            with self.server(reply,mode=mode,code=code) as (port,state):
                credentials,_=self.seed(case,port)
                for attempt in range(2):
                    with self.assertRaisesRegex(owner.OwnerUnavailable,'^PREPARED_OWNER_REPLY_NOT_VERIFIED$'):self.apply(case,credentials)
                self.assertEqual(len(state['requests']),1)
                self.assertFalse((directory/'prepared.owner-result.json').exists())
                self.assertEqual(reader.prepared_status(directory),'active')
                # A lost primary marker cannot turn a known send into an
                # ordinary terminal transaction or enable generic restore.
                (directory/'prepared.apply.pending.json').unlink()
                journal.atomic_json(directory/'result.json',{'status':'committed'})
                self.assertEqual(reader.prepared_status(directory),'active')
                with self.assertRaises(RuntimeError):journal.admit(directory.parent)

    def test_foreign_or_contradictory_reply_remains_debt(self):
        for kind in ('foreign','missing','wrong-status'):
            case=self.factory.make_case(kind);directory,intent,reply,view,effective=case
            wire=copy.deepcopy(reply)
            with self.server(wire,code=409 if kind=='wrong-status' else 200) as (port,state):
                credentials,_=self.seed(case,port)
                wire.update(reply)
                if kind=='foreign':wire['transaction_id']='foreign'
                elif kind=='missing':del wire['outcome']['KnownCommit']
                with self.assertRaises(owner.OwnerUnavailable):self.apply(case,credentials)
                self.assertEqual(len(state['requests']),1)
                self.assertFalse((directory/'prepared.owner-result.json').exists())
                self.assertEqual(reader.prepared_status(directory),'active')

    def test_oversized_duplicate_or_ambiguous_response_keeps_origin(self):
        for mode in ('oversized','duplicate-json','duplicate-header'):
            case=self.factory.make_case(mode);directory,intent,reply,view,effective=case
            with self.server(reply,mode=mode) as (port,state):
                credentials,_=self.seed(case,port)
                with self.assertRaises(owner.OwnerUnavailable):self.apply(case,credentials)
                self.assertEqual(len(state['requests']),1)
                self.assertFalse((directory/'prepared.owner-result.json').exists())
                self.assertEqual(reader.prepared_status(directory),'active')

    def test_location_authority_or_preimage_failure_is_before_any_send(self):
        changes=('remote','wildcard','tls','path','bad-port','missing-location','duplicate-location','node-sync','node-admin','monitor','disabled','expired','duplicate-token','template-changed','unknown-token')
        for kind in changes:
            case=self.factory.make_case(kind);directory,intent,reply,view,effective=case
            credentials,_=self.seed(case,12345)
            with closing(sqlite3.connect(intent['files']['database'])) as db:
                if kind in ('remote','wildcard'):db.execute('UPDATE settings SET value=? WHERE key=?',('example.invalid' if kind=='remote' else '0.0.0.0','webListen'))
                elif kind=='tls':db.execute('UPDATE settings SET value=? WHERE key=?',('/private/cert','webCertFile'))
                elif kind=='path':db.execute('UPDATE settings SET value=? WHERE key=?',('/../','webBasePath'))
                elif kind=='bad-port':db.execute('UPDATE settings SET value=? WHERE key=?',('012345','webPort'))
                elif kind=='missing-location':db.execute('DELETE FROM settings WHERE key=?',('webListen',))
                elif kind=='duplicate-location':db.execute('INSERT INTO settings(key,value) VALUES (?,?)',('webPort','12345'))
                elif kind in ('node-sync','node-admin','monitor'):db.execute('UPDATE api_tokens SET scope=?',(kind,))
                elif kind=='disabled':db.execute('UPDATE api_tokens SET enabled=0')
                elif kind=='expired':db.execute('UPDATE api_tokens SET expires_at=1')
                elif kind=='duplicate-token':db.execute('INSERT INTO api_tokens SELECT * FROM api_tokens')
                elif kind=='template-changed':db.execute('UPDATE settings SET value=? WHERE key=?',('{"changed":true}','xrayTemplateConfig'))
                elif kind=='unknown-token':db.execute('DELETE FROM api_tokens')
                db.commit()
            with mock.patch.object(owner.http.client,'HTTPConnection',side_effect=AssertionError('failed admission sent a request')):
                with self.assertRaises(owner.OwnerUnavailable):self.apply(case,credentials)
            self.assertFalse((directory/owner.DISPATCH).exists())

    def test_failed_dispatch_directory_barrier_never_sends_and_blocks_retry(self):
        case=self.factory.make_case();directory,intent,reply,view,effective=case
        credentials,_=self.seed(case,12345)
        real=journal.atomic_json
        def failed(path,value):
            real(path,value)
            if Path(path)==directory/owner.DISPATCH:raise OSError('synthetic dispatch barrier')
        with mock.patch.object(journal,'atomic_json',side_effect=failed),mock.patch.object(owner.http.client,'HTTPConnection',side_effect=AssertionError('failed dispatch barrier sent')):
            with self.assertRaises(owner.OwnerUnavailable):self.apply(case,credentials)
        self.assertTrue((directory/owner.DISPATCH).is_file())
        with mock.patch.object(owner.http.client,'HTTPConnection',side_effect=AssertionError('uncertain dispatch was retried')):
            with self.assertRaises(owner.OwnerUnavailable):self.apply(case,credentials)

    def test_unverified_different_reused_or_stale_peer_gets_no_authorization_bytes(self):
        for kind in ('fake','different','reused','stale'):
            case=self.factory.make_case(kind);directory,intent,reply,view,effective=case
            expected=intent['expected']
            before={**view,'native_generation':expected['generation'],'mutation_epoch':expected['epoch'],'effective_config_sha256':expected['before_sha256']}
            if kind=='different':before['native_generation']='00000000-0000-4000-8000-000000000099'
            observed=('fixture-boot',{'pid':123,'start_ticks':'100','parent_pid':1},None,{})
            peer=mock.Mock(side_effect=execution.ExecutionUnavailable('PANEL_PEER_NOT_VERIFIED')) if kind=='fake' else mock.Mock(side_effect=[(1,'fd',1,2,3),(2,'fd',1,2,3)] if kind=='reused' else [(1,'fd',1,2,3)]*2)
            with self.server(reply) as (port,state):
                credentials,_=self.seed(case,port)
                with mock.patch.object(execution,'_collected_evidence',return_value=nullcontext(((before,effective),observed))),mock.patch.object(execution,'_accepted_panel_peer',peer),mock.patch.object(execution,'_recheck',side_effect=execution.ExecutionUnavailable('PROCESS_CHANGED') if kind=='stale' else None),mock.patch.object(owner.http.client.HTTPConnection,'request',side_effect=AssertionError('credential bytes sent before verified peer')) as send:
                    with self.assertRaises(owner.OwnerUnavailable):self.apply(case,credentials,mock_peer=False)
                    send.assert_not_called()
                self.assertEqual(state['requests'],[],'unverified listener received HTTP/Authorization bytes')
                self.assertTrue((directory/owner.DISPATCH).is_file())
                self.assertFalse((directory/'prepared.owner-result.json').exists())
                self.assertEqual(reader.prepared_status(directory),'active')
    def test_private_template_reader_is_exact_readonly_and_does_not_heal(self):
        case=self.factory.make_case();directory,intent,reply,view,effective=case
        credentials,raw=self.seed(case,12345)
        before=Path(intent['files']['database']).read_bytes()
        self.assertEqual(completion._stored_template_raw(intent['files']['database']),raw)
        self.assertEqual(completion._stored_template(intent['files']['database']),reader.decode(raw))
        with completion._database_reader(intent['files']['database']) as db:
            with self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM settings')
        self.assertEqual(Path(intent['files']['database']).read_bytes(),before)

    def test_peer_checks_share_one_finite_accept_budget(self):
        case=self.factory.make_case();directory,intent,reply,view,effective=case
        expected=intent['expected']
        before={**view,'native_generation':expected['generation'],'mutation_epoch':expected['epoch'],'effective_config_sha256':expected['before_sha256']}
        observed=('fixture-boot',{'pid':123,'start_ticks':'100','parent_pid':1},None,{})
        with mock.patch.object(execution,'_collected_evidence',return_value=nullcontext(((before,effective),observed))),mock.patch.object(execution,'_accepted_panel_peer',return_value=(1,'fd',1,2,3)) as peer,mock.patch.object(execution,'_recheck'):
            with execution.verified_panel_socket(object(),expected):
                self.assertEqual(peer.call_count,2)
                self.assertEqual(peer.call_args_list[0].args[2],peer.call_args_list[1].args[2],'accept budget was renewed')
                self.assertLess(execution.time.monotonic(),peer.call_args_list[0].args[2])


if __name__=='__main__':unittest.main()
