"""Synthetic portable proof; actual Linux descriptor/namespace calls are NOT_RUN here."""
import contextlib
import copy
import genericpath
import gettext
import importlib.util
import io
import json
import pathlib
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))
from operations import node_transaction_readonly as m


def plan():
    return {'schema_version': 1, 'operation': 'synthetic-operation', 'pid': 123,
            'started_at': '2026-10-19T01:02:03Z'}


def binding():
    return {'schema': 1, 'target': m.owner.TARGETS[0], 'operation': 'a'*32,
            'namespace': {'mount': [9, 90], 'root': [7, 1]},
            'roots': [{'path': '/state/transactions', 'chain': [[7, 1], [7, 2], [7, 3]]}]}


def legacy_module():
    spec=importlib.util.spec_from_file_location('legacy_transactions',ROOT/'scripts/lib/transaction_state.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class Scan:
    def __init__(self, names): self.entries = [SimpleNamespace(name=n) for n in names]
    def __enter__(self): return iter(self.entries)
    def __exit__(self, *_): return False


class Posix:
    """Only the finite filesystem syscalls used by this observer; no OS fallthrough."""
    def __init__(self, statuses=('committed', 'rolled_back')):
        self.nodes, self.fds, self.opens, self.reads = {}, {}, [], []
        self.nextfd, self.nextino = 10, 4
        self.add('/', directory=True, ino=1)
        self.add('/state', directory=True, ino=2)
        self.add('/state/transactions', directory=True, ino=3)
        for i, status in enumerate(statuses):
            path = '/state/transactions/synthetic-'+str(i)
            self.add(path, directory=True)
            self.add(path+'/plan.json', value=plan())
            self.add(path+'/result.json', value={'status': status, 'message': 'PRIVATE_MARKER'})
        self.namespace = binding()['namespace']

    def add(self, path, *, directory=False, value=None, raw=None, ino=None, mode=None, uid=0, nlink=None):
        if ino is None: ino, self.nextino = self.nextino, self.nextino+1
        data = b'' if directory else raw if raw is not None else json.dumps(value).encode()
        self.nodes[path] = {'data': data, 'info': SimpleNamespace(st_dev=7, st_ino=ino,
            st_mode=mode if mode is not None else (stat.S_IFDIR | 0o700 if directory else stat.S_IFREG | 0o600),
            st_uid=uid, st_gid=0, st_nlink=nlink if nlink is not None else 2 if directory else 1,
            st_size=len(data), st_mtime_ns=1, st_ctime_ns=1)}

    def path(self, name, fd):
        if name == '/': return '/'
        base = self.fds[fd][0]
        return base if name == '.' else base.rstrip('/')+'/'+name

    def open(self, name, flags, dir_fd=None):
        path = self.path(name, dir_fd)
        self.opens.append((path, flags))
        node = self.nodes.get(path)
        if node is None: raise FileNotFoundError('PRIVATE_MARKER')
        if stat.S_ISLNK(node['info'].st_mode): raise OSError('PRIVATE_MARKER')
        if flags & m.os.O_DIRECTORY and not stat.S_ISDIR(node['info'].st_mode): raise OSError('PRIVATE_MARKER')
        fd, self.nextfd = self.nextfd, self.nextfd+1
        self.fds[fd] = (path, node, 0)
        return fd

    def fstat(self, fd): return self.fds[fd][1]['info']
    def stat(self, name, *, dir_fd=None, follow_symlinks=True): return self.nodes[self.path(name, dir_fd)]['info']
    def close(self, fd): del self.fds[fd]
    def read(self, fd, size):
        path, node, offset = self.fds[fd]
        self.reads.append(path)
        chunk = node['data'][offset:offset+size]
        self.fds[fd] = (path, node, offset+len(chunk))
        return chunk
    def scandir(self, fd):
        prefix = self.fds[fd][0].rstrip('/')+'/'
        return Scan([p[len(prefix):] for p in self.nodes if p.startswith(prefix) and '/' not in p[len(prefix):]])

    @contextlib.contextmanager
    def patched(self):
        # Flags are Linux values even on the portable host; all syscalls are stubbed.
        with contextlib.ExitStack() as stack:
            # Keep argparse/gettext and other stdlib users on the real process os.
            stack.enter_context(mock.patch.object(m, 'os', SimpleNamespace(**vars(m.os))))
            for key, value in {'O_DIRECTORY':0x10000, 'O_NOFOLLOW':0x20000, 'O_CLOEXEC':0x80000,
                               'O_NONBLOCK':0x800, 'O_NOATIME':0x40000}.items():
                stack.enter_context(mock.patch.object(m.os, key, value, create=True))
            for name in ('open', 'fstat', 'stat', 'close', 'read', 'scandir'):
                stack.enter_context(mock.patch.object(m.os, name, getattr(self, name)))
            stack.enter_context(mock.patch.object(m, 'require_native'))
            stack.enter_context(mock.patch.object(m.owner, 'namespace_identity', side_effect=lambda: copy.deepcopy(self.namespace)))
            yield self


class ObserverTests(unittest.TestCase):
    def observe(self, fs, value=None):
        with fs.patched():
            return m.observe(binding() if value is None else value, m.owner.TARGETS[0], 'a'*32)

    def assert_stop(self, fs, code=None, value=None):
        with self.assertRaises((m.Stop, OSError)) as stopped:
            self.observe(fs, value)
        if code is not None: self.assertEqual(stopped.exception.code, code)
        self.assertEqual(fs.fds, {})

    def test_default_inert_no_host_file_or_namespace_effect(self):
        for args in ([], ['--target', 'PRIVATE_MARKER', '--operation', 'PRIVATE_MARKER', '--binding-sha256', 'PRIVATE_MARKER']):
            with self.subTest(args=args), mock.patch.object(m, 'require_native', side_effect=AssertionError), \
                 mock.patch.object(m, 'read_binding', side_effect=AssertionError), \
                 mock.patch.object(m, 'observe', side_effect=AssertionError), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(m.main(args), 77)
            self.assertEqual(json.loads(out.getvalue()), {'status':'INERT_READONLY_ACTION_REQUIRED',
                             'filesystemWrites':0, 'liveAdmission':False})

    def test_positive_empty_and_terminal_observation_only(self):
        for statuses in ((), ('committed',), ('rolled_back',), ('committed', 'rolled_back')):
            fs = Posix(statuses)
            with self.subTest(statuses=statuses):
                result = self.observe(fs)
                self.assertEqual(result, {'status':'LOCAL_TRANSACTIONS_CLEAR_NOT_ADMISSION',
                    'counts':{'rootsObserved':1, 'transactionsObserved':len(statuses),
                              'committed':statuses.count('committed'), 'rolledBack':statuses.count('rolled_back')},
                    'rootSetCompleteness':'NOT_OBSERVED', 'operationalExclusion':'NOT_OBSERVED',
                    'liveAdmission':False, 'filesystemWrites':0})
                self.assertEqual(fs.fds, {})
                self.assertNotIn('PRIVATE_MARKER', json.dumps(result))
                self.assertEqual(set(fs.reads), {p for p in fs.nodes if p.endswith(('plan.json','result.json'))})
                self.assertTrue(all(not flags & (m.os.O_WRONLY | m.os.O_RDWR | m.os.O_CREAT | m.os.O_TRUNC)
                                    for _, flags in fs.opens))

    def test_unknown_status_gap_legacy_stays_compatible_new_observer_refuses(self):
        legacy = legacy_module()
        with tempfile.TemporaryDirectory() as root:
            child = pathlib.Path(root)/'synthetic-unknown'; child.mkdir()
            (child/'plan.json').write_text(json.dumps(plan()), encoding='utf-8')
            (child/'result.json').write_text(json.dumps({'status':'future_terminal'}), encoding='utf-8')
            self.assertEqual(legacy.incomplete(root), [])  # Reproduces old CLI admission gap.
        self.assert_stop(Posix(('future_terminal',)), 'STOP_TRANSACTION_STATUS_UNKNOWN')

    def test_actual_redacted_producers_terminal_compatibility_and_metadata_cap(self):
        legacy=legacy_module()
        for status in m.TERMINAL:
            for text in ('x','\U0001f600'):
                with self.subTest(status=status,text=text), tempfile.TemporaryDirectory() as root:
                    path=legacy.begin(root,text*80,2**31-1)
                    legacy.mark(path,status,text*160)
                    p=(path/'plan.json').read_bytes(); r=(path/'result.json').read_bytes()
                    self.assertLessEqual(len(p),m.MAX_METADATA_BYTES)
                    self.assertLessEqual(len(r),m.MAX_METADATA_BYTES)
                    self.assertEqual(m.metadata_status(m.parse_json(p,m.MAX_METADATA_BYTES),
                                                      m.parse_json(r,m.MAX_METADATA_BYTES)),status)

    def test_existing_agent_minimal_schema1_terminal_fixtures_remain_clear(self):
        for status in m.TERMINAL:
            fs=Posix((status,)); fs.add('/state/transactions/synthetic-0/plan.json',value={'schema_version':1})
            with self.subTest(status=status):
                self.assertEqual(self.observe(fs)['counts']['transactionsObserved'],1)

    def test_all_active_states_refused(self):
        for state in m.ACTIVE:
            with self.subTest(state=state): self.assert_stop(Posix((state,)), 'STOP_TRANSACTION_NONTERMINAL')

    def test_binding_schema_numbers_identity_and_authority_flags(self):
        changes = [('schema', True), ('namespace', {'mount':[9,True], 'root':[7,1]}),
                   ('namespace', {'mount':[9,90], 'root':[7,False]}), ('roots', []),
                   ('roots', [{'path':'/state/transactions', 'chain':[[7,1],[7,True],[7,3]]}]),
                   ('closed', True), ('noPending', True)]
        for key, value in changes:
            b = binding(); b[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(m.Stop):
                m.validate_binding(b, m.owner.TARGETS[0], 'a'*32)
        for target, operation in (('other','a'*32),(m.owner.TARGETS[0],'b'*32)):
            with self.assertRaises(m.Stop): m.validate_binding(binding(), target, operation)

    def test_unbound_missing_unsafe_duplicate_nested_or_excessive_roots_refused(self):
        roots = binding()['roots']
        values = [None, [], roots*5, roots*2,
                  [dict(path='/state/../transactions', chain=roots[0]['chain'])],
                  [dict(path='/state//transactions', chain=roots[0]['chain'])],
                  [dict(path='/state/transactions/', chain=roots[0]['chain'])],
                  roots+[dict(path='/state/transactions/child', chain=roots[0]['chain']+[[7,4]])],
                  [dict(path='/state/transactions', chain=[[7,1],[7,2]])]]
        for value in values:
            b = binding(); b['roots'] = value
            with self.subTest(value=value), self.assertRaises(m.Stop):
                m.validate_binding(b, m.owner.TARGETS[0], 'a'*32)
        fs = Posix(()); del fs.nodes['/state/transactions']; self.assert_stop(fs)

    def test_namespace_and_root_identity_mismatch_before_metadata_read(self):
        fs = Posix(); fs.namespace['mount'][1] += 1
        self.assert_stop(fs, 'STOP_NAMESPACE_CHANGED'); self.assertFalse(fs.reads)
        fs = Posix(); fs.nodes['/state']['info'].st_ino += 100
        self.assert_stop(fs, 'STOP_ROOT_IDENTITY'); self.assertFalse(fs.reads)

    def test_missing_plan_or_result_and_non_directory_child_refused(self):
        for name in ('plan.json','result.json'):
            fs = Posix(('committed',)); del fs.nodes['/state/transactions/synthetic-0/'+name]
            with self.subTest(name=name): self.assert_stop(fs)
        fs = Posix(()); fs.add('/state/transactions/plain', raw=b'private'); self.assert_stop(fs)

    def test_symlinks_at_ancestor_root_child_and_metadata_refused(self):
        for path in ('/state','/state/transactions','/state/transactions/synthetic-0',
                     '/state/transactions/synthetic-0/plan.json','/state/transactions/synthetic-0/result.json'):
            fs = Posix(('committed',)); fs.nodes[path]['info'].st_mode = stat.S_IFLNK | 0o777
            with self.subTest(path=path): self.assert_stop(fs)

    def test_unsafe_owner_mode_and_metadata_type_hardlink_refused(self):
        for path, field, value in (('/state','st_uid',1),('/state','st_mode',stat.S_IFDIR|0o777),
            ('/state/transactions/synthetic-0','st_gid',1),
            ('/state/transactions/synthetic-0/plan.json','st_mode',stat.S_IFIFO|0o600),
            ('/state/transactions/synthetic-0/plan.json','st_nlink',2),
            ('/state/transactions/synthetic-0/result.json','st_mode',stat.S_IFREG|0o644)):
            fs = Posix(('committed',)); setattr(fs.nodes[path]['info'],field,value)
            with self.subTest(path=path, field=field): self.assert_stop(fs)

    def test_strict_plan_and_result_schemas(self):
        plans = [None, [], {}, {**plan(),'schema_version':True}, {**plan(),'schema_version':2},
                 {**plan(),'pid':True}, {**plan(),'pid':0}, {**plan(),'payload':'PRIVATE_MARKER'}]
        results = [None, [], {}, {'status':True}, {'status':None}, {'status':'committed','extra':'PRIVATE_MARKER'},
                   {'status':'committed','message':False}, {'status':'committed','finished_at':12}]
        for value in plans:
            with self.subTest(plan=value), self.assertRaises(m.Stop): m.metadata_status(value,{'status':'committed'})
        for value in results:
            with self.subTest(result=value), self.assertRaises(m.Stop): m.metadata_status(plan(),value)

    def test_malformed_utf8_duplicate_json_fields_nonfinite_and_deep_json_refused(self):
        for raw in (b'PRIVATE_MARKER', b'\xff', b'{"status":"committed","status":"rolled_back"}',
                    b'{"status":NaN}', b'['*1500+b']'*1500):
            fs = Posix(('committed',)); fs.add('/state/transactions/synthetic-0/result.json', raw=raw)
            with self.subTest(raw=raw[:20]): self.assert_stop(fs)

    def test_file_size_and_actual_read_bounds(self):
        for size in (0, m.MAX_METADATA_BYTES+1):
            fs = Posix(('committed',)); fs.add('/state/transactions/synthetic-0/result.json', raw=b'x'*size)
            with self.subTest(size=size): self.assert_stop(fs, 'STOP_METADATA_SIZE')
        fs = Posix(('committed',))
        node = fs.nodes['/state/transactions/synthetic-0/result.json']; node['data'] = b'x'*(m.MAX_METADATA_BYTES+1)
        self.assert_stop(fs, 'STOP_METADATA_SIZE')
        fs = Posix(('committed',)); fs.nodes['/state/transactions/synthetic-0/result.json']['data'] = b'{}'
        self.assert_stop(fs, 'STOP_METADATA_CHANGED')

    def test_total_count_and_name_bounds(self):
        fs = Posix(());
        for i in range(m.MAX_TRANSACTIONS+1): fs.add('/state/transactions/n'+str(i), directory=True)
        self.assert_stop(fs, 'STOP_TRANSACTION_LIMIT'); self.assertFalse(fs.reads)
        fs = Posix(()); fs.add('/state/transactions/'+'x'*(m.MAX_NAME_BYTES+1), directory=True)
        self.assert_stop(fs, 'STOP_TRANSACTION_NAME')

    def test_exact_count_limit_and_total_across_multiple_bound_roots(self):
        fs = Posix(('committed',)*m.MAX_TRANSACTIONS)
        result = self.observe(fs)
        self.assertEqual(result['counts']['transactionsObserved'],m.MAX_TRANSACTIONS)
        fs.add('/other',directory=True)
        other = fs.nodes['/other']['info'].st_ino
        b = binding(); b['roots'].append({'path':'/other','chain':[[7,1],[7,other]]})
        # An empty second root does not consume another transaction allowance.
        self.assertEqual(self.observe(fs,b)['counts']['rootsObserved'],2)
        fs.add('/other/extra',directory=True)
        self.assert_stop(fs,'STOP_TRANSACTION_LIMIT',b)

    def test_backups_payload_files_are_never_opened_and_utf8_metadata_is_opaque(self):
        fs = Posix(('committed',)); child='/state/transactions/synthetic-0'
        value=plan(); value['operation']='синтетическая операция'
        fs.add(child+'/plan.json',value=value)
        fs.add(child+'/config.before.json',raw=b'PRIVATE_MARKER')
        fs.add(child+'/x-ui.before.db',raw=b'PRIVATE_MARKER')
        fs.add(child+'/request.json',raw=b'PRIVATE_MARKER')
        result=self.observe(fs)
        self.assertEqual(result['counts']['committed'],1)
        self.assertTrue(all(path.rsplit('/',1)[-1] in ('plan.json','result.json') for path in fs.reads))
        self.assertFalse(any(path.endswith(('config.before.json','x-ui.before.db','request.json')) for path,_ in fs.opens))

    def test_close_all_descriptors_and_preserve_primary_refusal(self):
        fs=Posix(('future_terminal',)); original=fs.close; calls=[]
        def close(fd):
            path=fs.fds[fd][0]
            original(fd); calls.append(fd)
            if path.endswith('/result.json'): raise OSError('PRIVATE_MARKER')
        fs.close=close
        self.assert_stop(fs,'STOP_TRANSACTION_STATUS_UNKNOWN')
        self.assertGreater(len(calls),1)
        fs=Posix(()); original=fs.close
        def close_failed(fd):
            original(fd)
            if not fs.fds: raise OSError('PRIVATE_MARKER')
        fs.close=close_failed
        self.assert_stop(fs,'STOP_OBSERVATION_IO')

    def test_root_child_file_rebinds_and_metadata_drift_before_final_acceptance(self):
        for path in ('/state','/state/transactions','/state/transactions/synthetic-0',
                     '/state/transactions/synthetic-0/result.json'):
            fs = Posix(('committed',)); original = fs.read
            def read(fd, size):
                raw = original(fd,size)
                if fs.fds[fd][0].endswith('result.json') and not raw:
                    old = fs.nodes[path]
                    fs.nodes[path] = {'data':old['data'],'info':copy.copy(old['info'])}
                    fs.nodes[path]['info'].st_ino += 1000
                return raw
            fs.read = read
            with self.subTest(path=path): self.assert_stop(fs)
        fs = Posix(('committed',)); original = fs.read
        def changed(fd,size):
            raw = original(fd,size)
            if not raw: fs.fds[fd][1]['info'].st_ctime_ns += 1
            return raw
        fs.read = changed; self.assert_stop(fs, 'STOP_METADATA_CHANGED')

    def test_root_listing_and_namespace_change_after_reads_refused(self):
        for change in ('entry','namespace'):
            fs = Posix(('committed',)); original = fs.read
            def read(fd,size):
                raw = original(fd,size)
                if fs.fds[fd][0].endswith('result.json') and not raw:
                    if change == 'entry': fs.add('/state/transactions/new',directory=True)
                    else: fs.namespace['mount'][1] += 1
                return raw
            fs.read = read
            with self.subTest(change=change): self.assert_stop(fs)

    def test_cooperative_deadline_refusal_closes_descriptors(self):
        fs = Posix()
        with fs.patched(), mock.patch.object(m.time,'monotonic',side_effect=[0,0,6]):
            with self.assertRaises(m.Stop) as stopped: m.observe(binding(),m.owner.TARGETS[0],'a'*32)
        self.assertEqual(stopped.exception.code,'STOP_OBSERVATION_TIMEOUT'); self.assertEqual(fs.fds,{})

    def test_read_binding_regular_private_stable_size_hash_only(self):
        raw = json.dumps(binding()).encode()
        info = SimpleNamespace(st_dev=7,st_ino=1,st_mode=stat.S_IFREG|0o600,st_uid=0,st_gid=0,
                               st_nlink=1,st_size=len(raw),st_mtime_ns=1,st_ctime_ns=1)
        with mock.patch.object(m.os,'fstat',return_value=info), \
             mock.patch.object(m.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(raw))):
            self.assertEqual(m.read_binding(m.hashlib.sha256(raw).hexdigest(),m.owner.TARGETS[0],'a'*32),binding())
        for field,value in (('st_mode',stat.S_IFIFO|0o600),('st_mode',stat.S_IFREG|0o644),('st_uid',1),('st_nlink',2),('st_size',0)):
            unsafe=copy.copy(info); setattr(unsafe,field,value)
            with self.subTest(field=field), mock.patch.object(m.os,'fstat',return_value=unsafe), self.assertRaises(m.Stop):
                m.read_binding('b'*64,m.owner.TARGETS[0],'a'*32)

    def test_binding_hash_size_and_descriptor_drift_refused(self):
        raw=json.dumps(binding()).encode()
        info=SimpleNamespace(st_dev=7,st_ino=1,st_mode=stat.S_IFREG|0o600,st_uid=0,st_gid=0,
                             st_nlink=1,st_size=len(raw),st_mtime_ns=1,st_ctime_ns=1)
        for change in ('hash','size','descriptor'):
            after=copy.copy(info)
            if change=='descriptor': after.st_ino+=1
            supplied=raw+b' ' if change=='size' else raw
            expected='b'*64 if change=='hash' else m.hashlib.sha256(supplied).hexdigest()
            with self.subTest(change=change), mock.patch.object(m.os,'fstat',side_effect=[info,after]), \
                 mock.patch.object(m.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(supplied))), self.assertRaises(m.Stop):
                m.read_binding(expected,m.owner.TARGETS[0],'a'*32)

    def test_main_success_is_sanitized_counts_not_binding_ids(self):
        fs=Posix()
        with fs.patched(), mock.patch.object(m,'read_binding',return_value=binding()), \
             contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(m.main(['--observe-readonly','--target',m.owner.TARGETS[0],
                                     '--operation','a'*32,'--binding-sha256','b'*64]),0)
        result=json.loads(out.getvalue())
        self.assertEqual(result['counts']['transactionsObserved'],2)
        for value in ('PRIVATE_MARKER','a'*32,m.owner.TARGETS[0],'/state','synthetic-0'):
            self.assertNotIn(value,out.getvalue()+err.getvalue())
        self.assertEqual(err.getvalue(),''); self.assertLess(len(out.getvalue()),1024)

    def test_posix_mock_is_scoped_and_main_survives_gettext_catalog_lookup(self):
        process_os=m.os; process_stat=process_os.stat
        with Posix().patched():
            self.assertIsNot(m.os,process_os)
            self.assertIs(process_os.stat,process_stat)
        # POSIX exists calls os.stat; Windows' builtin exists otherwise hides this.
        with mock.patch.dict(process_os.environ,{'LANGUAGE':'en_US.UTF-8'}), \
             mock.patch.object(gettext.os.path,'exists',wraps=genericpath.exists) as exists:
            self.test_main_success_is_sanitized_counts_not_binding_ids()
        self.assertGreater(exists.call_count,0)

    def test_native_guard_refuses_before_binding_on_unsupported_host(self):
        with mock.patch.object(m.sys,'platform','not-linux'), mock.patch.object(m,'read_binding',side_effect=AssertionError), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(m.main(['--observe-readonly']),78)
        self.assertEqual(json.loads(out.getvalue())['status'],'STOP_NATIVE_LINUX_ROOT_REQUIRED')

    def test_sanitized_main_errors_unknown_arguments_no_stderr_or_marker(self):
        changed=m.Stop('STOP_PLAN_SCHEMA'); changed.code='PRIVATE_MARKER'
        cases = [(['--PRIVATE_MARKER'],None), (['--observe-readonly'],m.Stop('PRIVATE_MARKER')),
                 (['--observe-readonly'],changed),
                 (['--observe-readonly'],OSError('PRIVATE_MARKER')),
                 (['--observe-readonly'],RuntimeError('PRIVATE_MARKER')),
                 (['--observe-readonly'],m.Stop('STOP_TRANSACTION_STATUS_UNKNOWN'))]
        for args,error in cases:
            with self.subTest(args=args,error=type(error).__name__), mock.patch.object(m,'require_native'), \
                 mock.patch.object(m,'read_binding',side_effect=error) as load, \
                 contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(m.main(args),78)
            result=json.loads(out.getvalue()); self.assertFalse(result['liveAdmission'])
            self.assertEqual(result['filesystemWrites'],0); self.assertIn(result['status'],m.STOP_CODES)
            self.assertNotIn('PRIVATE_MARKER',out.getvalue()+err.getvalue()); self.assertEqual(err.getvalue(),'')
            if error is None: load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
