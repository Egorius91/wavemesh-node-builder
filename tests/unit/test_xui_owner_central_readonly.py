"""Portable collector proof only. No Docker/PostgreSQL/native runtime access."""
import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import stat
import time
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location('central_readonly', ROOT / 'operations/xui_owner_central_readonly.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def binding():
    return {'schema': 1, 'contractSha256': m.CONTRACT_SHA256,
            'targetManifestSha256': m.TARGET_MANIFEST_SHA256, 'operation': 'a' * 32,
            'targets': {name: {'tenantId': 'fixture-tenant', 'nodeId': 'fixture-node-' + str(i),
                              'externalId': 'fixture-external-' + str(i)} for i, name in enumerate(m.TARGETS)},
            'api': {'containerId': 'b' * 64, 'imageId': 'sha256:' + 'c' * 64},
            'postgres': {'containerId': 'd' * 64, 'imageId': 'sha256:' + 'e' * 64},
            'dockerSha256': 'f' * 64}


def snapshot():
    return dict(schema_applicable=True, query_readonly=True, query_repeatable_read=True,
                **{k: (1 if k == 'exact_target_count' else 0) for k in m.COUNTS})


def identity(pin, pid=123):
    return ('%s %s %s 2026-10-19T01:02:03.123Z true 0\n' %
            (pin['containerId'], pin['imageId'], pid)).encode()


class Reader:
    def __init__(self, value=None):
        self.value = value if value is not None else snapshot()
        self.calls = []
        self.changed = False

    def __call__(self, args, data, deadline):
        self.calls.append((args, data, deadline))
        if args == m.PSQL_ARGS:
            return json.dumps(self.value).encode()
        name = 'api' if args[-1] == m.API else 'postgres'
        return identity(binding()[name], 456 if self.changed and len(self.calls) > 3 else 123)


class Pipe:
    def __init__(self, fd): self.fd = fd; self.closed = False
    def fileno(self): return self.fd
    def close(self): self.closed = True


class FakeProcess:
    def __init__(self, rc=0):
        self.stdin, self.stdout, self.stderr = Pipe(11), Pipe(12), Pipe(13)
        self.rc, self.returncode, self.killed = rc, None, False
    def poll(self): return self.returncode
    def wait(self, timeout): self.returncode = self.rc; return self.rc
    def kill(self): self.killed = True; self.returncode = -9


class FakeSelector:
    def __init__(self): self.mapping = {}
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def register(self, stream, event): self.mapping[stream] = event
    def unregister(self, stream): del self.mapping[stream]
    def get_map(self): return self.mapping
    def select(self, timeout): return [(SimpleNamespace(fileobj=stream), event) for stream, event in list(self.mapping.items())]


class CollectorTests(unittest.TestCase):
    def collect(self, reader):
        return m.collect(binding(), m.TARGETS[0], 'a' * 32, m.load_schema(), reader,
                         time.monotonic() + m.OUTER_SECONDS)

    def test_default_is_inert_before_file_or_native_action(self):
        with mock.patch.object(m, 'read_binding', side_effect=AssertionError), \
             mock.patch.object(m, 'load_schema', side_effect=AssertionError), \
             mock.patch.object(m, 'NativeDocker', side_effect=AssertionError), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(m.main([]), 77)
        self.assertEqual(json.loads(output.getvalue()),
                         {'status': 'INERT_READONLY_ACTION_REQUIRED', 'serverWrites': 0})

    def test_default_remains_inert_with_binding_looking_arguments(self):
        with mock.patch.object(m, 'read_binding', side_effect=AssertionError), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['--target', m.TARGETS[0], '--operation', 'a' * 32,
                                     '--binding-sha256', 'b' * 64]), 77)

    def test_frozen_target_and_contract_binding(self):
        self.assertIs(m.validate_binding(binding(), m.TARGETS[0], 'a' * 32).__class__, dict)
        mutations = [('schema', True), ('contractSha256', '0' * 64),
                     ('targetManifestSha256', '0' * 64), ('operation', 'b' * 32),
                     ('dockerSha256', '../docker'), ('targets', {})]
        for key, value in mutations:
            with self.subTest(key=key), self.assertRaises(m.Stop):
                row = binding(); row[key] = value
                m.validate_binding(row, m.TARGETS[0], 'a' * 32)
        with self.assertRaises(m.Stop):
            m.validate_binding(binding(), 'arbitrary-host', 'a' * 32)

    def test_extra_binding_authority_or_fields_rejected(self):
        for key in ('closed', 'quiescent', 'sql', 'flags', 'sourceRevision', 'lease'):
            value = binding(); value[key] = True
            with self.subTest(key=key), self.assertRaises(m.Stop):
                m.validate_binding(value, m.TARGETS[0], 'a' * 32)

    def test_colliding_targets_rejected_before_reader(self):
        for field in ('nodeId', 'externalId'):
            value = binding()
            value['targets'][m.TARGETS[1]][field] = value['targets'][m.TARGETS[0]][field]
            reader = mock.Mock(side_effect=AssertionError)
            with self.subTest(field=field), self.assertRaises(m.Stop):
                m.collect(value, m.TARGETS[0], 'a' * 32, {}, reader, time.monotonic() + 20)
            reader.assert_not_called()

    def test_private_ids_are_hex_psql_stdin_only(self):
        value = binding()
        marker = "fixture'\\\n\\!touch SHOULD_NEVER_RUN\n"  # build layer also has no SQL escaping seam
        value['targets'][m.TARGETS[0]]['nodeId'] = marker
        sql = m.build_sql(value, m.TARGETS[0], m.load_schema())
        self.assertNotIn(marker.encode(), sql)
        self.assertIn(marker.encode().hex().encode(), sql)
        self.assertIn(b"decode(:'node_hex','hex')", sql)
        self.assertNotIn(marker, repr(m.PSQL_ARGS))
        self.assertIn('--host=/var/run/postgresql --port=5432', m.PSQL_ARGS[-1])
        with self.assertRaises(m.Stop):
            m.validate_binding(value, m.TARGETS[0], 'a' * 32)  # control characters are also refused

    def test_quote_backslash_and_unicode_do_not_change_psql_grammar(self):
        value = binding()
        value['targets'][m.TARGETS[0]]['nodeId'] = "fixture'\\\u041d\u043e\u0434\u0430"
        m.validate_binding(value, m.TARGETS[0], 'a' * 32)
        sql = m.build_sql(value, m.TARGETS[0], m.load_schema())
        declarations = sql.split(b'BEGIN TRANSACTION', 1)[0].splitlines()
        self.assertEqual(len(declarations), 4)
        for line in declarations:
            self.assertRegex(line.decode(), r'^\\set (tenant|node|external|schema)_hex [a-f0-9]+$')

    def test_positive_snapshot_is_not_admission_and_has_no_private_ids(self):
        reader = Reader()
        result = self.collect(reader)
        self.assertEqual(result['status'], 'SNAPSHOT_CLEAR_NOT_ADMISSION')
        self.assertIs(result['liveAdmission'], False)
        for key in ('nodeTransactions', 'runtimeSource', 'configPolicyAuthority', 'operationalExclusion'):
            self.assertEqual(result[key], 'NOT_OBSERVED')
        self.assertEqual(result['serverWrites'], 0)
        self.assertEqual(len(reader.calls), 5)
        self.assertEqual([call[0][-1] for call in (reader.calls[0], reader.calls[1], reader.calls[3], reader.calls[4])],
                         [m.API, m.POSTGRES, m.API, m.POSTGRES])
        for row in binding()['targets'].values():
            for private in row.values():
                self.assertNotIn(private, json.dumps(result))
        for args, data, _ in reader.calls:
            self.assertNotIn('fixture-', repr(args))
            self.assertEqual(data is not None, args == m.PSQL_ARGS)

    def test_every_debt_count_blocks_and_fence_is_informational(self):
        for key in m.COUNTS:
            value = snapshot(); value[key] = 2 if key == 'exact_target_count' else 1
            with self.subTest(key=key):
                status = self.collect(Reader(value))['status']
                self.assertEqual(status, 'SNAPSHOT_CLEAR_NOT_ADMISSION' if key == 'held_binding_fences' else 'SNAPSHOT_BLOCKED')
        value = snapshot(); value['exact_target_count'] = 0
        self.assertEqual(self.collect(Reader(value))['status'], 'SNAPSHOT_BLOCKED')

    def test_all_count_fields_reject_bool_negative_float_string_and_overflow(self):
        for field in m.COUNTS:
            for bad in (True, False, -1, 1.0, '0', None, m.MAX_COUNT + 1):
                value = snapshot(); value[field] = bad
                with self.subTest(field=field, bad=bad), self.assertRaises(m.Stop):
                    m.validate_snapshot(value)

    def test_missing_unknown_and_malformed_transaction_fields_refused(self):
        for key in snapshot():
            value = snapshot(); del value[key]
            with self.subTest(missing=key), self.assertRaises(m.Stop):
                m.validate_snapshot(value)
        value = snapshot(); value['payload'] = {}
        with self.assertRaises(m.Stop):
            m.validate_snapshot(value)
        for field in ('query_readonly', 'query_repeatable_read', 'schema_applicable'):
            for bad in (False, 0, 1, None, 'true'):
                value = snapshot(); value[field] = bad
                with self.subTest(field=field, bad=bad), self.assertRaises(m.Stop):
                    m.validate_snapshot(value)

    def test_schema_mismatch_stops_without_aggregate_acceptance(self):
        with self.assertRaisesRegex(m.Stop, 'STOP_SCHEMA_NOT_APPLICABLE'):
            self.collect(Reader({'schema_applicable': False}))

    def test_json_duplicate_unknown_truncation_and_multirecord_refused(self):
        for raw in (b'{"query_readonly":true,"query_readonly":true}', b'{}\n{}', b'{',
                    b'\xff', b'{"x":NaN}', b'x' * (m.MAX_BYTES+1), b''):
            with self.subTest(raw=raw[:30]), self.assertRaises(m.Stop):
                m.parse_json(raw)

    def test_api_or_database_drift_after_snapshot_refused(self):
        reader = Reader(); reader.changed = True
        with self.assertRaisesRegex(m.Stop, 'STOP_IDENTITY_CHANGED_DURING_READ'):
            self.collect(reader)

    def test_unexpected_container_or_stopped_process_refused(self):
        for raw in (identity(binding()['api']).replace(b'true', b'false'),
                    identity(binding()['api']).replace(b' 123 ', b' 0 '),
                    identity(binding()['postgres']), b'null', b'x' * 1025):
            with self.subTest(raw=raw[:40]), self.assertRaises(m.Stop):
                m.parse_identity(raw, binding()['api'])

    def test_reader_timeout_or_error_is_not_retried(self):
        for code in ('STOP_OBSERVATION_TIMEOUT', 'STOP_OBSERVATION_READ_FAILED', 'STOP_OBSERVATION_SIZE'):
            reader = mock.Mock(side_effect=m.Stop(code))
            with self.subTest(code=code), self.assertRaisesRegex(m.Stop, code):
                self.collect(reader)
            self.assertEqual(reader.call_count, 1)

    def test_expired_overall_snapshot_refused(self):
        with self.assertRaisesRegex(m.Stop, 'STOP_OBSERVATION_TIMEOUT'):
            m.collect(binding(), m.TARGETS[0], 'a' * 32, m.load_schema(), Reader(), time.monotonic() - 1)

    def test_fixed_sql_readonly_bounds_and_no_mutating_primitives(self):
        sql = m.build_sql(binding(), m.TARGETS[0], m.load_schema()).decode()
        self.assertIn('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;', sql)
        self.assertIn("statement_timeout='5s'", sql)
        self.assertIn("lock_timeout='2s'", sql)
        self.assertIn("idle_in_transaction_session_timeout='7s'", sql)
        self.assertTrue(sql.endswith('ROLLBACK;\n'))
        for forbidden in ('FOR UPDATE', 'pg_advisory', 'lock_node_runtime_binding', 'INSERT ', 'UPDATE ', 'DELETE ',
                          'pg_stat_activity', 'certificateDeliveryPayload" FROM', 'SELECT *'):
            self.assertNotIn(forbidden, sql)
        self.assertLess(sql.index('schema_applicable'), sql.index('PREPARE node_owner_snapshot'))
        self.assertIn('\\if :schema_applicable', sql)
        self.assertIn('\\else', sql)

    def test_dispatched_terminal_failures_and_unscoped_work_are_preserved(self):
        sql = m.AGGREGATE_SQL
        self.assertIn("(c.attempts>0 AND c.status<>'SUCCEEDED')", sql)
        self.assertIn('a."completedAt" IS NULL', sql)
        self.assertIn('a."activeNodeKey" IS NOT NULL', sql)
        self.assertIn("r.operation='bot.accesses.create' AND r.status='PROCESSING'", sql)
        self.assertIn("'ACTIVATE_PAID_SUBSCRIPTION','DISABLE_EXPIRED_VPN_ACCOUNT','SYNC_VPN_ACCOUNT'", sql)
        self.assertIn("j.status IN ('PENDING','PROCESSING')", sql)

    def test_schema_asset_is_content_pinned_and_limited_to_used_fields(self):
        document = json.loads((ROOT/'operations/xui_owner_central_schema.json').read_bytes())
        database = m.load_schema()
        self.assertEqual(document['schemaAndMigrationFiles'], 59)
        self.assertEqual(database['migration_rows'], 57)
        self.assertEqual(len(database['migrations']), 57)
        self.assertEqual(len(database['columns']), 31)
        self.assertEqual(len(database['enums']), 6)
        with mock.patch.object(m.Path, 'read_bytes', return_value=b'{}'), self.assertRaisesRegex(m.Stop, 'STOP_COLLECTOR_SCHEMA_PIN'):
            m.load_schema()

    def test_transport_only_accepts_fixed_arguments_before_popen(self):
        transport = m.NativeDocker.__new__(m.NativeDocker)
        transport.fd = -1
        with mock.patch.object(m.subprocess, 'Popen', side_effect=AssertionError) as process:
            for args in (('exec', 'foreign', 'psql'), ('inspect', m.API), ('version',)):
                with self.subTest(args=args), self.assertRaisesRegex(m.Stop, 'STOP_TRANSPORT_ARGS'):
                    transport(args, None, time.monotonic()+20)
            with self.assertRaisesRegex(m.Stop, 'STOP_OBSERVATION_TIMEOUT'):
                transport(m.PSQL_ARGS, b'anything', time.monotonic()-1)
            process.assert_not_called()

    def transport_fixture(self, stdout, stderr=None, rc=0, clock=None):
        proc = FakeProcess(rc)
        queue = {12: list(stdout) + [b''], 13: list(stderr or []) + [b'']}
        transport = m.NativeDocker.__new__(m.NativeDocker); transport.fd = 9
        writes = bytearray()
        def write(fd, data):
            self.assertEqual(fd, 11)
            writes.extend(data)
            return len(data)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(m.subprocess, 'Popen', return_value=proc))
        stack.enter_context(mock.patch.object(m.selectors, 'DefaultSelector', FakeSelector))
        stack.enter_context(mock.patch.object(m.os, 'set_blocking'))
        stack.enter_context(mock.patch.object(m.os, 'read', side_effect=lambda fd, size: queue[fd].pop(0)))
        stack.enter_context(mock.patch.object(m.os, 'write', side_effect=write))
        if clock is not None:
            stack.enter_context(mock.patch.object(m.time, 'monotonic', side_effect=clock))
        return stack, proc, transport, writes

    def test_bounded_pipe_transport_streams_stdin_without_export(self):
        stack, proc, transport, writes = self.transport_fixture([b'{"safe":', b'true}'])
        with stack, mock.patch.object(m.subprocess, 'Popen', return_value=proc) as popen:
            result = transport(m.PSQL_ARGS, b'fixture-private-only-on-stdin' * 300, time.monotonic()+20)
        self.assertEqual(result, b'{"safe":true}')
        self.assertEqual(writes, b'fixture-private-only-on-stdin' * 300)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], '/proc/self/fd/9')
        self.assertNotIn('private', repr(args))
        self.assertNotIn('DATABASE_URL', kwargs['env'])
        self.assertEqual(kwargs['env']['DOCKER_HOST'], 'unix:///var/run/docker.sock')
        self.assertEqual(kwargs['pass_fds'], (9,))
        self.assertTrue(all(p.closed for p in (proc.stdin, proc.stdout, proc.stderr)))
        self.assertFalse(proc.killed)

    def test_bounded_pipe_stderr_and_nonzero_refuse_without_error_egress(self):
        for stderr, rc in (([b'fixture-PRIVATE-ID password'], 0), ([], 1)):
            stack, proc, transport, _ = self.transport_fixture([b'{}'], stderr, rc)
            with stack, self.assertRaisesRegex(m.Stop, '^STOP_OBSERVATION_READ_FAILED$'):
                transport(m.PSQL_ARGS, b'SELECT', time.monotonic()+20)
            self.assertTrue(all(p.closed for p in (proc.stdin, proc.stdout, proc.stderr)))

    def test_bounded_pipe_oversize_kills_and_closes(self):
        stack, proc, transport, _ = self.transport_fixture([b'x' * 4096] * 5)
        with stack, self.assertRaisesRegex(m.Stop, 'STOP_OBSERVATION_SIZE'):
            transport(m.PSQL_ARGS, b'SELECT', time.monotonic()+20)
        self.assertTrue(proc.killed)
        self.assertTrue(all(p.closed for p in (proc.stdin, proc.stdout, proc.stderr)))

    def test_bounded_pipe_deadline_kills_without_retry(self):
        stack, proc, transport, _ = self.transport_fixture([], clock=[0, 21])
        with stack, self.assertRaisesRegex(m.Stop, 'STOP_OBSERVATION_TIMEOUT'):
            transport(m.PSQL_ARGS, b'SELECT', 20)
        self.assertTrue(proc.killed)

    def test_existing_repair_production_seam_unchanged(self):
        repair = (ROOT/'operations/xui_owner_repair.py').read_text()
        self.assertIn('LIVE_ADMISSION_UNBOUND_NOT_READY', repair)
        self.assertNotIn('xui_owner_central_readonly', repair)

    def test_main_sanitizes_native_error_and_private_input(self):
        with mock.patch.object(m.sys, 'platform', 'linux'), \
             mock.patch.object(m.os, 'geteuid', create=True, return_value=0), \
             mock.patch.object(m, 'read_binding', side_effect=OSError('fixture-PRIVATE-CREDENTIAL')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(m.main(['--collect-readonly', '--target', m.TARGETS[0], '--operation', 'a'*32]), 78)
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'STOP_OBSERVATION_UNKNOWN')
        self.assertNotIn('PRIVATE', output.getvalue())

    def test_private_binding_fd_permissions_hash_and_strict_input(self):
        raw = json.dumps(binding()).encode()
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_gid=0, st_nlink=1, st_size=len(raw))
        with mock.patch.object(m.os, 'fstat', return_value=info), \
             mock.patch.object(m.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw))):
            self.assertEqual(m.read_binding(m.sha256(raw), m.TARGETS[0], 'a'*32), binding())
        for key, bad in (('st_mode', stat.S_IFREG | 0o644), ('st_mode', stat.S_IFIFO | 0o600),
                         ('st_uid', 1001), ('st_gid', 1001), ('st_nlink', 2), ('st_size', m.MAX_BYTES+1)):
            changed = copy.copy(info); setattr(changed, key, bad)
            with self.subTest(key=key, bad=bad), mock.patch.object(m.os, 'fstat', return_value=changed), \
                 mock.patch.object(m.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw))), \
                 self.assertRaisesRegex(m.Stop, 'STOP_BINDING_PRIVATE_INPUT'):
                m.read_binding(m.sha256(raw), m.TARGETS[0], 'a'*32)
        with mock.patch.object(m.os, 'fstat', return_value=info), \
             mock.patch.object(m.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw))), \
             self.assertRaisesRegex(m.Stop, 'STOP_BINDING_HASH'):
            m.read_binding('0'*64, m.TARGETS[0], 'a'*32)

    def test_main_blocked_snapshot_returns78_and_clear_returns0_without_repair(self):
        for status, expected in (('SNAPSHOT_BLOCKED', 78), ('SNAPSHOT_CLEAR_NOT_ADMISSION', 0)):
            fake = mock.Mock()
            with self.subTest(status=status), mock.patch.object(m.sys, 'platform', 'linux'), \
                 mock.patch.object(m.os, 'geteuid', create=True, return_value=0), \
                 mock.patch.object(m, 'read_binding', return_value=binding()), \
                 mock.patch.object(m, 'NativeDocker', return_value=fake), \
                 mock.patch.object(m, 'collect', return_value={'status': status, 'liveAdmission': False}), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(m.main(['--collect-readonly', '--target', m.TARGETS[0], '--operation', 'a'*32,
                                         '--binding-sha256', 'b'*64]), expected)
                self.assertIs(json.loads(output.getvalue())['liveAdmission'], False)
            fake.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
