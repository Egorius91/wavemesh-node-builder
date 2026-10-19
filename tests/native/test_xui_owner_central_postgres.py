#!/usr/bin/env python3
"""Finite synthetic SQL/catalog proof, default77; explicit future CI native only.

No staging names, real mapping, full migrations or application bootstrap. Actual
collector SQL/parser run against one owned PostgreSQL16 container; inspect is an
explicit fixture stub. This never qualifies production NativeDocker or admission.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).absolute().parents[2]
IMAGE = 'postgres:16'  # existing SaaS CI convention; resolve once, create by ID
LABEL = 'io.wavemesh.central-sql-fixture'
PREFIX = 'wm-central-sql-'
MAX_OUTPUT = 16384
PROOF_SECONDS = 180
EXPECTED_CASES = 46
DOCKER = '/usr/bin/docker'
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C.UTF-8',
       'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONFIG': '/nonexistent'}


class Failure(RuntimeError):
    pass


def need(condition, code):
    if not condition:
        raise Failure(code)


def load_collector():
    spec = importlib.util.spec_from_file_location('central_native_collector', ROOT/'operations/xui_owner_central_readonly.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def literal(value):
    """Synthetic/source data only; no handwritten SQL string escaping."""
    return "convert_from(decode('" + value.encode().hex() + "','hex'),'UTF8')"


def identifier(value):
    need(type(value) is str and re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', value), 'FIXTURE_IDENTIFIER')
    return '"' + value + '"'


def fixture_binding(m):
    return {'schema': 1, 'contractSha256': m.CONTRACT_SHA256,
            'targetManifestSha256': m.TARGET_MANIFEST_SHA256, 'operation': 'a'*32,
            'targets': {target: {'tenantId': 'synthetic-tenant',
                'nodeId': "synthetic'\\\u041d\u043e\u0434\u0430-" + str(i),
                'externalId': "synthetic-external'\\\u0412\u043d\u0435\u0448\u043d\u0438\u0439-" + str(i)}
                for i, target in enumerate(m.TARGETS)},
            'api': {'containerId': 'b'*64, 'imageId': 'sha256:'+'c'*64},
            'postgres': {'containerId': 'd'*64, 'imageId': 'sha256:'+'e'*64}, 'dockerSha256': 'f'*64}


def fixture_sql(schema):
    """Only31 used columns/6enums and synthetic57 migration metadata rows.

    The metadata intentionally does not mean migrations/triggers were applied.
    There are no extra application constraints, writers or private row contents.
    """
    tables = {}
    for key, (kind, required, typmod) in schema['columns'].items():
        table, column = key.split('.')
        if kind == 'timestamp':
            need(typmod == 3, 'FIXTURE_TIMESTAMP_PRECISION')
            datatype = 'timestamp(3) without time zone'
        elif kind in ('text', 'int4'):
            datatype = kind
        else:
            need(kind in schema['enums'], 'FIXTURE_TYPE')
            datatype = 'public.' + identifier(kind)
        tables.setdefault(table, []).append(identifier(column) + ' ' + datatype + (' NOT NULL' if required else ''))
    statements = ['DROP SCHEMA public CASCADE;', 'CREATE SCHEMA public;']
    for name, labels in schema['enums'].items():
        need(all(re.fullmatch('[A-Z_]+', label) for label in labels), 'FIXTURE_ENUM')
        statements.append('CREATE TYPE public.' + identifier(name) + " AS ENUM ('" + "','".join(labels) + "');")
    for name, columns in tables.items():
        statements.append('CREATE TABLE public.' + identifier(name) + ' (' + ','.join(columns) + ');')
    statements.append('CREATE TABLE public._prisma_migrations (migration_name text,checksum text,'
        'finished_at timestamptz,rolled_back_at timestamptz,applied_steps_count integer);')
    rows = ['(' + literal(name) + ',' + literal(checksum) + ',now(),NULL,1)' for name, checksum in schema['migrations'].items()]
    statements.append('INSERT INTO public._prisma_migrations VALUES ' + ','.join(rows) + ';')
    return '\n'.join(statements).encode()


def insert(table, **values):
    def sql_value(value):
        if value is None:
            return 'NULL'
        if type(value) is int:
            return str(value)
        need(type(value) is str, 'FIXTURE_VALUE')
        if value in ('FIXTURE_NOW_MINUS_MINUTE', 'FIXTURE_NOW_PLUS_MINUTE'):
            return "now()" + ("-" if value.endswith('MINUS_MINUTE') else "+") + "interval '1 minute'"
        return literal(value)
    enum_columns = {'NodeCommand.status':'NodeCommandStatus','CommandAttempt.status':'NodeCommandStatus',
        'VpnAccess.status':'AccessLifecycleStatus','VpnAccess.reconciliationStatus':'ReconciliationStatus',
        'InternalRequest.status':'InternalRequestStatus','OutboxJob.type':'OutboxJobType','OutboxJob.status':'OutboxJobStatus'}
    encoded = [sql_value(value) + ('::public.'+identifier(enum_columns[table+'.'+key])
               if table+'.'+key in enum_columns else '') for key,value in values.items()]
    return ('INSERT INTO public.' + identifier(table) + ' (' + ','.join(identifier(k) for k in values) +
            ') VALUES (' + ','.join(encoded) + ');')


def debt_cases(binding, target):
    row = binding['targets'][target]
    tenant, node = row['tenantId'], row['nodeId']
    command = dict(id='synthetic-command', tenantId=tenant, targetNodeId=node, status='SUCCEEDED', attempts=0)
    cases = []
    def add(name, table, values, expected):
        cases.append((name, insert(table, **values), expected))
    for status in ('PENDING', 'LEASED', 'RUNNING', 'RETRYABLE_FAILED'):
        expected = {'nonterminal_commands': 1}
        if status in ('LEASED', 'RUNNING'): expected['dispatched_unresolved'] = 1
        add('command_'+status, 'NodeCommand', dict(command, status=status), expected)
    for status in ('PERMANENT_FAILED', 'ROLLED_BACK', 'ROLLBACK_FAILED', 'CANCELLED'):
        add('attempted_'+status, 'NodeCommand', dict(command, status=status, attempts=1), {'dispatched_unresolved': 1})
    add('succeeded_attempted', 'NodeCommand', dict(command, attempts=1), {})
    add('cross_tenant', 'NodeCommand', dict(command, tenantId='synthetic-other-tenant'), {'target_tenant_anomalies': 1})
    for status, completed in (('LEASED', 'FIXTURE_NOW_MINUS_MINUTE'), ('RUNNING', 'FIXTURE_NOW_MINUS_MINUTE'),
                              ('SUCCEEDED', None), ('SUCCEEDED', 'FIXTURE_NOW_MINUS_MINUTE')):
        payload = insert('NodeCommand', **command) + insert('CommandAttempt', commandId=command['id'], status=status, completedAt=completed)
        cases.append(('attempt_'+status+('_null' if completed is None else '_complete'), payload,
                      {} if status == 'SUCCEEDED' and completed is not None else {'unfinished_attempts': 1}))
    access = dict(tenantId=tenant, assignedEntryNodeId=node, status='READY', reconciliationStatus='IN_SYNC', desiredVersion=1, observedVersion=1)
    for name, change in (('materializing', {'status':'MATERIALIZING'}), ('applying', {'reconciliationStatus':'APPLYING'}),
                         ('version_mismatch', {'observedVersion':0})):
        add(name, 'VpnAccess', dict(access, **change), {'unsettled_accesses':1})
    add('issuance_active', 'NodeCertificateIssuanceAttempt', {'nodeId':node,'activeNodeKey':'synthetic-active'}, {'issuance_requires_reconciliation':1})
    add('issuance_inactive', 'NodeCertificateIssuanceAttempt', {'nodeId':node,'activeNodeKey':None}, {})
    credential = dict(nodeId=node, certificateAcknowledgedAt=None, certificateDeliveryPayload='synthetic-not-a-credential',
                      certificateDeliveryExpiresAt='FIXTURE_NOW_PLUS_MINUTE')
    add('delivery_pending', 'NodeCredential', credential, {'pending_delivery':1})
    add('delivery_expired', 'NodeCredential', dict(credential, certificateDeliveryExpiresAt='FIXTURE_NOW_MINUS_MINUTE'), {})
    add('delivery_acknowledged', 'NodeCredential', dict(credential, certificateAcknowledgedAt='FIXTURE_NOW_MINUS_MINUTE'), {})
    add('delivery_absent', 'NodeCredential', dict(credential, certificateDeliveryPayload=None), {})
    add('request_processing', 'InternalRequest', {'tenantId':tenant,'operation':'bot.accesses.create','status':'PROCESSING'}, {'unscoped_provisioning_requests':1})
    add('request_other_tenant', 'InternalRequest', {'tenantId':'synthetic-other','operation':'bot.accesses.create','status':'PROCESSING'}, {})
    for job_type in ('ACTIVATE_PAID_SUBSCRIPTION', 'DISABLE_EXPIRED_VPN_ACCOUNT', 'SYNC_VPN_ACCOUNT'):
        for status in ('PENDING', 'PROCESSING'):
            add('outbox_'+job_type+'_'+status, 'OutboxJob', {'type':job_type,'status':status}, {'unscoped_node_effect_jobs':1})
    add('outbox_done', 'OutboxJob', {'type':'SYNC_VPN_ACCOUNT','status':'DONE'}, {})
    add('outbox_other_type', 'OutboxJob', {'type':'SEND_PAID_EXPIRY_REMINDER','status':'PENDING'}, {})
    add('held_fence', 'NodeRuntimeBindingFence', {'tenantId':tenant,'nodeId':node,'releasedAt':None}, {'held_binding_fences':1})
    add('released_fence', 'NodeRuntimeBindingFence', {'tenantId':tenant,'nodeId':node,'releasedAt':'FIXTURE_NOW_MINUS_MINUTE'}, {})
    return cases


def run_command(args, data=None, *, timeout=20, deadline=None, allow_error=False):
    if deadline is not None:
        timeout = min(timeout, deadline-time.monotonic())
    need(timeout > 0, 'FIXTURE_DEADLINE')
    result = subprocess.run([DOCKER, *args], input=data, capture_output=True, env=ENV, cwd='/',
                            timeout=timeout, check=False)
    need(len(result.stdout) <= MAX_OUTPUT and len(result.stderr) <= MAX_OUTPUT, 'FIXTURE_OUTPUT_LIMIT')
    if not allow_error:
        need(result.returncode == 0, 'FIXTURE_COMMAND_FAILED')
    return result


def private_state(path, value=None):
    """One exclusive root-private receipt; updates preserve its exact inode."""
    if value is not None and not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 0 and
             stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1 and info.st_size <= 4096,
             'FIXTURE_STATE_UNSAFE')
        if value is not None:
            raw = json.dumps(value, sort_keys=True).encode()
            need(len(raw) <= 4096, 'FIXTURE_STATE_SIZE')
            os.ftruncate(fd, 0); need(os.write(fd, raw) == len(raw), 'FIXTURE_STATE_WRITE'); os.fsync(fd)
            return value
        raw = os.read(fd, 4097)
        value = json.loads(raw)
        need(type(value) is dict and set(value) == {'schema','token','name','imageId','containerId','phase'}, 'FIXTURE_STATE_SCHEMA')
        need(type(value['schema']) is int and value['schema'] == 1 and re.fullmatch('[a-f0-9]{32}', value['token']) and
             value['name'] == PREFIX + value['token'] and value['phase'] in ('PREPARED','CREATED','CLEANED') and
             (value['imageId'] is None or re.fullmatch('sha256:[a-f0-9]{64}',value['imageId'])) and
             (value['containerId'] is None or re.fullmatch('[a-f0-9]{64}',value['containerId'])), 'FIXTURE_STATE_BINDING')
        return value
    finally:
        os.close(fd)


def owned_container(state, *, run=run_command):
    by_name = run(['container','ls','-aq','--no-trunc','--filter','name=^/'+state['name']+'$']).stdout.splitlines()
    by_label = run(['container','ls','-aq','--no-trunc','--filter','label='+LABEL+'='+state['token']]).stdout.splitlines()
    need(by_name == by_label and len(by_name) <= 1, 'FIXTURE_CONTAINER_OWNERSHIP_UNKNOWN')
    if not by_name:
        return None
    cid = by_name[0].decode('ascii')
    need(re.fullmatch('[a-f0-9]{64}',cid) and (state['containerId'] is None or state['containerId'] == cid), 'FIXTURE_CONTAINER_REBOUND')
    fmt = '{{.Id}} {{.Image}} {{index .Config.Labels "' + LABEL + '"}} {{.Name}}'
    fields = run(['inspect','--type','container','--format',fmt,cid]).stdout.decode('ascii').strip().split(' ')
    need(fields == [cid,state['imageId'],state['token'],'/'+state['name']], 'FIXTURE_CONTAINER_REBOUND')
    return cid


def finalize(path, *, run=run_command):
    if not path.exists():
        return {'status':'NATIVE_SQL_NOT_CREATED','zeroOwnedContainers':True}
    deadline = time.monotonic()+30
    def bounded(args):
        return run(args,deadline=deadline,timeout=5)
    state = private_state(path)
    cid = owned_container(state, run=bounded)
    if cid is not None:
        # Exact fresh identity/label, no volume deletion, recreation or retry.
        bounded(['rm','--force',cid])
    need(owned_container(state, run=bounded) is None, 'FIXTURE_RESIDUE')
    state['phase'] = 'CLEANED'; private_state(path,state)
    return {'status':'NATIVE_SQL_CLEANED','zeroOwnedContainers':True}


class PostgresFixture:
    def __init__(self, cid, m, deadline):
        self.cid, self.m, self.deadline = cid, m, deadline
        self.schema, self.binding = m.load_schema(), fixture_binding(m)
        self.target = m.TARGETS[0]
        self.calls = 0
        self.cases = 0
        self.last_sql_error = None

    def args(self):
        return ['exec','-i',self.cid,'psql','-X','-q','-t','-A','-v','ON_ERROR_STOP=1',
                '-v','VERBOSITY=sqlstate','-v','SHOW_CONTEXT=never',
                '--host=/var/run/postgresql','--port=5432','--username=fixture','--dbname=fixture']

    def sql(self, data, *, allow_error=False, timeout=20):
        return run_command(self.args(), data if type(data) is bytes else data.encode(),
                           timeout=timeout, deadline=self.deadline, allow_error=allow_error)

    def reset(self, rebuild=False):
        if rebuild:
            self.sql(fixture_sql(self.schema))
        else:
            tables = sorted({key.split('.')[0] for key in self.schema['columns']})
            self.sql('TRUNCATE '+','.join('public.'+identifier(t) for t in tables)+';')
        self.sql(''.join(insert('Node', id=r['nodeId'], tenantId=r['tenantId'], externalId=r['externalId'])
                         for r in self.binding['targets'].values()))

    def fingerprint(self):
        allowed = {key.split('.')[0] for key in self.schema['columns']} | {'_prisma_migrations'}
        tables = json.loads(self.sql("SELECT json_agg(tablename ORDER BY tablename) FROM pg_catalog.pg_tables "
                                    "WHERE schemaname='public';").stdout)
        need(type(tables) is list and tables and set(tables) <= allowed, 'FIXTURE_TABLE_SCOPE')
        # Only a single digest leaves PostgreSQL; synthetic contents never leave.
        fragments = ["coalesce((SELECT string_agg(v::text,',' ORDER BY v::text) FROM "
                     '(SELECT to_jsonb(t) AS v FROM public.'+identifier(t)+' t) s),\'\')' for t in tables]
        result = self.sql('SELECT md5('+"||'|'||".join(fragments)+');').stdout.strip()
        need(re.fullmatch(b'[a-f0-9]{32}', result), 'FIXTURE_FINGERPRINT')
        return result

    def no_sessions(self):
        raw = self.sql("SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE datname=current_database() "
            "AND backend_type='client backend' AND pid<>pg_backend_pid();").stdout.strip()
        need(raw == b'0', 'FIXTURE_BACKEND_RESIDUE')

    def reader(self, args, data, deadline):
        if args == self.m.PSQL_ARGS:
            self.calls += 1
            result = self.sql(data, allow_error=True)
            if result.returncode or result.stderr:
                self.last_sql_error = result.stderr
                raise self.m.Stop('STOP_OBSERVATION_READ_FAILED')
            return result.stdout
        # Explicit stub: SQL acceptance only, no production Docker proof.
        need(args[0] == 'inspect', 'FIXTURE_INSPECT_STUB_SCOPE')
        pin = self.binding['api' if args[-1] == self.m.API else 'postgres']
        return (pin['containerId']+' '+pin['imageId']+' 123 2026-10-19T00:00:00Z true 0\n').encode()

    def snapshot(self):
        before, calls = self.fingerprint(), self.calls
        result = self.m.collect(self.binding,self.target,'a'*32,self.schema,self.reader,
                                min(self.deadline,time.monotonic()+self.m.OUTER_SECONDS))
        need(self.calls == calls+1 and self.fingerprint() == before, 'FIXTURE_COLLECTOR_DML_OR_RETRY')
        need(result['liveAdmission'] is False and result['nodeTransactions'] == 'NOT_OBSERVED' and
             result['configPolicyAuthority'] == 'NOT_OBSERVED', 'FIXTURE_ADMISSION_OVERCLAIM')
        self.no_sessions()
        self.cases += 1
        return result

    def expect(self, overrides):
        expected = {name:1 if name == 'exact_target_count' else 0 for name in self.m.COUNTS}
        expected.update(overrides)
        result = self.snapshot()
        need({key:result['snapshot'][key] for key in self.m.COUNTS} == expected, 'FIXTURE_COUNT_MISMATCH')
        blocked = any(expected[key] != (1 if key == 'exact_target_count' else 0)
                      for key in self.m.COUNTS if key != 'held_binding_fences')
        need(result['status'] == ('SNAPSHOT_BLOCKED' if blocked else 'SNAPSHOT_CLEAR_NOT_ADMISSION'), 'FIXTURE_STATUS_MISMATCH')

    def refusal(self):
        before, calls = self.fingerprint(), self.calls
        try:
            self.snapshot()
        except self.m.Stop as error:
            need(error.code == 'STOP_SCHEMA_NOT_APPLICABLE', 'FIXTURE_WRONG_SCHEMA_REFUSAL')
        else:
            raise Failure('FIXTURE_SCHEMA_WAS_ACCEPTED')
        need(self.calls == calls+1 and self.fingerprint() == before, 'FIXTURE_REFUSAL_DML_OR_RETRY')
        self.no_sessions(); self.cases += 1

    def lock_timeout(self):
        before = self.fingerprint()
        args = [DOCKER,*self.args()]
        holder = subprocess.Popen(args, stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=ENV,cwd='/')
        try:
            holder.stdin.write(b'BEGIN; LOCK public."Node" IN ACCESS EXCLUSIVE MODE; SELECT 1; SELECT pg_sleep(4); ROLLBACK;\n')
            holder.stdin.close()
            ready,_,_ = select.select([holder.stdout],[],[],min(10,self.deadline-time.monotonic()))
            need(ready and holder.stdout.readline().strip() == b'1', 'FIXTURE_LOCK_NOT_OBSERVED')
            calls = self.calls
            started = time.monotonic()
            try:
                self.m.collect(self.binding,self.target,'a'*32,self.schema,self.reader,
                               min(self.deadline,time.monotonic()+self.m.OUTER_SECONDS))
            except self.m.Stop as error:
                need(error.code == 'STOP_OBSERVATION_READ_FAILED', 'FIXTURE_WRONG_TIMEOUT_RESULT')
            else:
                raise Failure('FIXTURE_LOCK_TIMEOUT_NOT_ENFORCED')
            need(self.calls == calls+1 and 1.5 <= time.monotonic()-started < 7 and
                 self.last_sql_error is not None and b'55P03' in self.last_sql_error, 'FIXTURE_TIMEOUT_RETRY_OR_BOUND')
            need(holder.wait(timeout=10) == 0, 'FIXTURE_LOCK_HOLDER_FAILED')
            need(not holder.stderr.read(1024), 'FIXTURE_LOCK_HOLDER_FAILED')
            need(self.fingerprint() == before, 'FIXTURE_TIMEOUT_DML')
            self.no_sessions(); self.cases += 1
        finally:
            if holder.poll() is None:
                holder.kill(); holder.wait(timeout=5)
            holder.stdin.close()
            holder.stdout.close(); holder.stderr.close()

    def exercise(self):
        self.reset(rebuild=True)
        self.expect({})  # real read_only/isolation + quoted/backslash/UTF-8 target
        row = self.binding['targets'][self.target]
        for field in ('tenantId','externalId'):
            saved = row[field]; row[field] = saved+'-wrong'
            try: self.expect({'exact_target_count':0})
            finally: row[field] = saved
        for _, sql, expected in debt_cases(self.binding,self.target):
            self.reset(); self.sql(sql); self.expect(expected)
        schema_cases = [
            'ALTER TABLE public."Node" DROP COLUMN "externalId";',
            'DROP TABLE public."NodeCredential";',
            "ALTER TYPE public.\"NodeCommandStatus\" ADD VALUE 'SYNTHETIC_EXTRA';",
            "UPDATE public._prisma_migrations SET checksum='synthetic-wrong';",
            'DELETE FROM public._prisma_migrations WHERE migration_name=(SELECT min(migration_name) FROM public._prisma_migrations);',
            'UPDATE public._prisma_migrations SET finished_at=NULL;',
        ]
        for sql in schema_cases:
            self.reset(rebuild=True); self.sql(sql); self.refusal()
        self.reset(rebuild=True); self.lock_timeout()
        self.expect({}); self.no_sessions()
        need(self.cases == self.calls == EXPECTED_CASES, 'FIXTURE_CASE_BUDGET_MISMATCH')
        return {'nativeCases':self.cases,'debtCases':len(debt_cases(self.binding,self.target)),
                'schemaRefusals':len(schema_cases),'readOnlyRepeatableRead':True,
                'noCollectorDml':True,'noCollectorRetry':True,'zeroClientBackendResidue':True,
                'fixtureInspect':'STUBBED','productionNativeDocker':'NOT_RUN',
                'actual57MigrationsApplied':False,'stagingSchema':'NOT_OBSERVED',
                'liveAdmission':False,'concurrentWriterProof':'NOT_RUN_NOT_REQUIRED'}


def new_state(path):
    need(not path.exists(), 'FIXTURE_STATE_COLLISION')
    token = uuid.uuid4().hex
    state = dict(schema=1,token=token,name=PREFIX+token,imageId=None,containerId=None,phase='PREPARED')
    private_state(path,state)
    return state


def prepare(path, state, deadline):
    def bounded(args):
        return run_command(args,deadline=deadline)
    need(owned_container(state,run=bounded) is None, 'FIXTURE_NAME_COLLISION')
    run_command(['pull','--quiet',IMAGE],timeout=60,deadline=deadline)
    metadata = run_command(['image','inspect','--format','{{.Id}} {{json .Config.Volumes}}',IMAGE],deadline=deadline).stdout.decode('ascii').strip().split(' ',1)
    need(len(metadata) == 2 and set(json.loads(metadata[1]) or {}) == {'/var/lib/postgresql/data'}, 'FIXTURE_IMAGE_VOLUME_CONTRACT')
    image = metadata[0]
    need(re.fullmatch('sha256:[a-f0-9]{64}',image), 'FIXTURE_IMAGE_ID')
    state['imageId'] = image; private_state(path,state)
    result = run_command(['create','--name',state['name'],'--label',LABEL+'='+state['token'],
        '--network','none','--memory','256m','--cpus','1','--pids-limit','128','--read-only',
        '--security-opt','no-new-privileges:true','--tmpfs','/tmp:rw,nosuid,noexec,size=16m',
        '--tmpfs','/var/run/postgresql:rw,nosuid,noexec,size=16m',
        '--tmpfs','/var/lib/postgresql/data:rw,nosuid,noexec,size=128m',
        '-e','POSTGRES_HOST_AUTH_METHOD=trust','-e','POSTGRES_USER=fixture','-e','POSTGRES_DB=fixture',
        image,'postgres','-c','listen_addresses=','-c','shared_buffers=16MB',
        '-c','log_statement=none','-c','log_min_error_statement=panic'],deadline=deadline)
    cid = result.stdout.decode('ascii').strip()
    need(re.fullmatch('[a-f0-9]{64}',cid), 'FIXTURE_CREATE_OUTCOME_UNKNOWN')
    state['containerId'] = cid; state['phase'] = 'CREATED'; private_state(path,state)
    need(owned_container(state,run=bounded) == cid, 'FIXTURE_CREATE_BINDING')
    run_command(['start',cid],deadline=deadline)
    for _ in range(30):
        ready = run_command(['exec',cid,'sh','-c','test "$(cat /proc/1/comm)" = postgres && exec '
                             'pg_isready --host=/var/run/postgresql --username=fixture --dbname=fixture'],
                            allow_error=True,deadline=deadline)
        if ready.returncode == 0:
            return cid,image
        time.sleep(0.2)
    raise Failure('FIXTURE_POSTGRES_NOT_READY')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--require-native',action='store_true')
    action.add_argument('--finalize',action='store_true')
    parser.add_argument('--state',type=Path)
    args = parser.parse_args(argv)
    if not args.require_native and not args.finalize:
        print('{"status":"NATIVE_SQL_NOT_RUN","effects":0}'); return 77
    result, failure, cleaned, state_owned = {}, None, False, False
    try:
        need(sys.platform == 'linux' and os.geteuid() == 0 and sys.version_info >= (3,12), 'FIXTURE_LINUX_ROOT_PY312_REQUIRED')
        need(args.state is not None and args.state.is_absolute(), 'FIXTURE_STATE_PATH')
        if args.finalize:
            print(json.dumps(finalize(args.state),sort_keys=True)); return 0
        m = load_collector()
        deadline = time.monotonic()+PROOF_SECONDS
        state = new_state(args.state); state_owned = True
        cid,image = prepare(args.state,state,deadline)
        result = PostgresFixture(cid,m,deadline).exercise()
        result.update(status='NATIVE_SQL_SYNTHETIC_PASSED',imageId=image)
    except Exception as error:
        failure = str(error) if isinstance(error,Failure) else 'FIXTURE_UNKNOWN_'+type(error).__name__.upper()
    finally:
        if state_owned:
            try:
                cleaned = finalize(args.state)['zeroOwnedContainers']
            except Exception:
                cleaned = False
    if failure is None and not cleaned:
        failure = 'FIXTURE_CLEANUP_NOT_PROVEN'
    if failure is not None:
        result = {'status':'NATIVE_SQL_FAILED','reason':failure}
    result['zeroOwnedContainers'] = cleaned
    print(json.dumps(result,sort_keys=True))
    return int(failure is not None or not cleaned)


if __name__ == '__main__':
    raise SystemExit(main())
