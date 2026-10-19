#!/usr/bin/env python3
"""Fixed staging aggregate reader, never a repair admission or writer fence.

Default is inert 77. Only --collect-readonly reads the prepared root-private
binding on stdin and invokes the fixed local Docker/psql channel. Private IDs
stay in this process/psql stdin; no credentials, Env, argv or payload are read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time

TARGETS = ('wm-stage-entry', 'wm-exit-2', 'wm-exit-de-frankfurt-2')
CONTRACT_SHA256 = '8edfcf278e464f05e491526e79ae0782582c891d270303c08835940334bf1d89'
TARGET_MANIFEST_SHA256 = '09ac3fca07cd05e5ba86b723c6c32fac1bf9687afce6c379c3999313429dd4ef'
SCHEMA_SHA256 = 'c45df01c5fe5b88c797eb6ae845ab58eb4654c79e2c2caf869d920cc42034eb8'
DOCKER = '/usr/bin/docker'
API = 'wavevpn-stage-api-1'
POSTGRES = 'wavevpn-stage-postgres-1'
MAX_BYTES = 16384
MAX_COUNT = 9223372036854775807
OUTER_SECONDS = 20
COUNTS = ('exact_target_count', 'target_tenant_anomalies', 'nonterminal_commands',
          'dispatched_unresolved', 'unfinished_attempts', 'unsettled_accesses',
          'issuance_requires_reconciliation', 'pending_delivery',
          'unscoped_provisioning_requests', 'unscoped_node_effect_jobs', 'held_binding_fences')


class Stop(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise Stop(code)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def pairs(items):
    value = {}
    for key, item in items:
        require(key not in value, 'STOP_DUPLICATE_JSON_FIELD')
        value[key] = item
    return value


def parse_json(raw):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, 'STOP_OBSERVATION_SIZE')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise Stop('STOP_OBSERVATION_JSON') from None


def hex_string(value, length):
    return type(value) is str and re.fullmatch('[a-f0-9]{' + str(length) + '}', value) is not None


def validate_binding(value, target, operation):
    require(target in TARGETS and hex_string(operation, 32), 'STOP_TARGET_OR_OPERATION')
    require(type(value) is dict and set(value) == {
        'schema', 'contractSha256', 'targetManifestSha256', 'operation', 'targets',
        'api', 'postgres', 'dockerSha256'}, 'STOP_BINDING_SCHEMA')
    require(type(value['schema']) is int and value['schema'] == 1 and
            value['contractSha256'] == CONTRACT_SHA256 and
            value['targetManifestSha256'] == TARGET_MANIFEST_SHA256 and
            value['operation'] == operation, 'STOP_BINDING_CONTRACT')
    require(hex_string(value['dockerSha256'], 64), 'STOP_BINDING_BINARY')
    targets = value['targets']
    require(type(targets) is dict and set(targets) == set(TARGETS), 'STOP_BINDING_TARGETS')
    for row in targets.values():
        require(type(row) is dict and set(row) == {'tenantId', 'nodeId', 'externalId'}, 'STOP_BINDING_TARGETS')
        # IDs are opaque UTF-8 strings, not a SQL/psql/shell grammar. Hex encoding
        # below and psql's quoted substitution own transport and literal quoting.
        require(all(type(v) is str and 0 < len(v.encode('utf-8')) <= 256 and
                    all(ord(c) >= 32 and ord(c) != 127 for c in v) for v in row.values()),
                'STOP_BINDING_ID')
    require(len({r['nodeId'] for r in targets.values()}) == len(TARGETS) and
            len({r['externalId'] for r in targets.values()}) == len(TARGETS), 'STOP_BINDING_TARGET_COLLISION')
    for name in ('api', 'postgres'):
        pin = value[name]
        require(type(pin) is dict and set(pin) == {'containerId', 'imageId'} and
                hex_string(pin['containerId'], 64) and type(pin['imageId']) is str and
                pin['imageId'].startswith('sha256:') and hex_string(pin['imageId'][7:], 64),
                'STOP_BINDING_CONTAINER')
    return value


def validate_snapshot(value):
    require(type(value) is dict, 'STOP_SNAPSHOT_SCHEMA')
    if value == {'schema_applicable': False}:
        raise Stop('STOP_SCHEMA_NOT_APPLICABLE')
    require(set(value) == {'schema_applicable', 'query_readonly', 'query_repeatable_read', *COUNTS},
            'STOP_SNAPSHOT_SCHEMA')
    require(value['schema_applicable'] is True, 'STOP_SCHEMA_NOT_APPLICABLE')
    require(value['query_readonly'] is True and value['query_repeatable_read'] is True,
            'STOP_TRANSACTION_NOT_READONLY_REPEATABLE_READ')
    require(all(type(value[k]) is int and 0 <= value[k] <= MAX_COUNT for k in COUNTS),
            'STOP_SNAPSHOT_COUNT')
    return value


def snapshot_clear(value):
    validate_snapshot(value)
    return value['exact_target_count'] == 1 and all(value[k] == 0 for k in COUNTS
        if k not in ('exact_target_count', 'held_binding_fences'))


# All fields/predicates are the frozen CONTRACT aggregate. Additional booleans
# attest the transaction/schema, without exposing catalog rows or migration IDs.
AGGREGATE_SQL = '''
PREPARE node_owner_snapshot(text,text,text) AS
SELECT json_build_object(
 'schema_applicable',true,
 'query_readonly',current_setting('transaction_read_only')='on',
 'query_repeatable_read',current_setting('transaction_isolation')='repeatable read',
 'exact_target_count',(SELECT count(*) FROM public."Node" n WHERE n.id=$2 AND n."tenantId"=$1 AND n."externalId"=$3),
 'target_tenant_anomalies',(SELECT count(*) FROM public."NodeCommand" c WHERE c."targetNodeId"=$2 AND c."tenantId"<>$1),
 'nonterminal_commands',(SELECT count(*) FROM public."NodeCommand" c WHERE c."targetNodeId"=$2 AND c.status IN ('PENDING','LEASED','RUNNING','RETRYABLE_FAILED')),
 'dispatched_unresolved',(SELECT count(*) FROM public."NodeCommand" c WHERE c."targetNodeId"=$2 AND (c.status IN ('LEASED','RUNNING') OR (c.attempts>0 AND c.status<>'SUCCEEDED'))),
 'unfinished_attempts',(SELECT count(*) FROM public."CommandAttempt" a JOIN public."NodeCommand" c ON c.id=a."commandId" WHERE c."targetNodeId"=$2 AND (a.status IN ('LEASED','RUNNING') OR a."completedAt" IS NULL)),
 'unsettled_accesses',(SELECT count(*) FROM public."VpnAccess" a WHERE a."tenantId"=$1 AND a."assignedEntryNodeId"=$2 AND (a.status='MATERIALIZING' OR a."reconciliationStatus"='APPLYING' OR a."desiredVersion"<>a."observedVersion")),
 'issuance_requires_reconciliation',(SELECT count(*) FROM public."NodeCertificateIssuanceAttempt" a WHERE a."nodeId"=$2 AND a."activeNodeKey" IS NOT NULL),
 'pending_delivery',(SELECT count(*) FROM public."NodeCredential" c WHERE c."nodeId"=$2 AND c."certificateAcknowledgedAt" IS NULL AND c."certificateDeliveryPayload" IS NOT NULL AND c."certificateDeliveryExpiresAt">now()),
 'unscoped_provisioning_requests',(SELECT count(*) FROM public."InternalRequest" r WHERE r."tenantId"=$1 AND r.operation='bot.accesses.create' AND r.status='PROCESSING'),
 'unscoped_node_effect_jobs',(SELECT count(*) FROM public."OutboxJob" j WHERE j.type IN ('ACTIVATE_PAID_SUBSCRIPTION','DISABLE_EXPIRED_VPN_ACCOUNT','SYNC_VPN_ACCOUNT') AND j.status IN ('PENDING','PROCESSING')),
 'held_binding_fences',(SELECT count(*) FROM public."NodeRuntimeBindingFence" f WHERE f."tenantId"=$1 AND f."nodeId"=$2 AND f."releasedAt" IS NULL));
EXECUTE node_owner_snapshot(
 convert_from(decode(:'tenant_hex','hex'),'UTF8'),
 convert_from(decode(:'node_hex','hex'),'UTF8'),
 convert_from(decode(:'external_hex','hex'),'UTF8'));
'''

# Catalog data is compared inside the read-only transaction. The required
# column/type/null/typmod and enum sets plus the exact migration/checksum set
# are public source pins, not assertions supplied by an operator Boolean.
SCHEMA_SQL = '''
WITH expected AS (SELECT convert_from(decode(:'schema_hex','hex'),'UTF8')::jsonb AS v),
actual AS (SELECT jsonb_build_object(
 'migration_rows',(SELECT count(*) FROM public._prisma_migrations),
 'migrations',(SELECT jsonb_object_agg(migration_name,checksum) FROM public._prisma_migrations
   WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL AND applied_steps_count>0),
 'columns',(SELECT jsonb_object_agg(c.relname||'.'||a.attname,
    jsonb_build_array(t.typname,a.attnotnull,a.atttypmod))
   FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
   JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
   JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
   JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace, expected e
   WHERE n.nspname='public' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
     AND tn.nspname IN ('public','pg_catalog') AND e.v->'columns' ? (c.relname||'.'||a.attname)),
 'enums',(SELECT jsonb_object_agg(name,labels) FROM (
   SELECT t.typname AS name,jsonb_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels
   FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace
   JOIN pg_catalog.pg_enum e ON e.enumtypid=t.oid, expected x
   WHERE n.nspname='public' AND x.v->'enums' ? t.typname GROUP BY t.typname) s)
) AS v)
SELECT actual.v=expected.v AS schema_applicable FROM actual,expected
\\gset
\\if :schema_applicable
'''


def build_sql(binding, target, schema):
    row = binding['targets'][target]
    encoded = {'tenant_hex': row['tenantId'].encode().hex(),
               'node_hex': row['nodeId'].encode().hex(),
               'external_hex': row['externalId'].encode().hex(),
               'schema_hex': json.dumps(schema, sort_keys=True, separators=(',', ':')).encode().hex()}
    # Only lowercase hex reaches psql metacommand grammar, and :'name' is the
    # standard psql SQL literal substitution, including for the public schema.
    require(all(re.fullmatch('[a-f0-9]+', v) for v in encoded.values()), 'STOP_ENCODING')
    header = ''.join('\\set ' + k + ' ' + v + '\n' for k, v in encoded.items())
    return (header + "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
            "SET LOCAL statement_timeout='5s';\nSET LOCAL lock_timeout='2s';\n"
            "SET LOCAL idle_in_transaction_session_timeout='7s';\n"
            "SET LOCAL search_path=pg_catalog,public;\n" + SCHEMA_SQL + AGGREGATE_SQL +
            "\\else\nSELECT '{\"schema_applicable\":false}'::json;\n\\endif\nROLLBACK;\n").encode()


INSPECT_FORMAT = '{{.Id}} {{.Image}} {{.State.Pid}} {{.State.StartedAt}} {{.State.Running}} {{.RestartCount}}'
PSQL_ARGS = ('exec', '-i', POSTGRES, 'sh', '-c',
             'exec psql -X -q -t -A -v ON_ERROR_STOP=1 --host=/var/run/postgresql --port=5432 '
             '--username="$POSTGRES_USER" --dbname="$POSTGRES_DB"')


def parse_identity(raw, pin):
    require(type(raw) is bytes and len(raw) < 1024, 'STOP_CONTAINER_IDENTITY')
    try:
        parts = raw.decode('ascii').strip().split(' ')
    except UnicodeError:
        raise Stop('STOP_CONTAINER_IDENTITY') from None
    require(len(parts) == 6 and parts[0] == pin['containerId'] and parts[1] == pin['imageId'] and
            re.fullmatch('[1-9][0-9]{0,9}', parts[2]) and
            re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z', parts[3]) and
            parts[4] == 'true' and re.fullmatch('[0-9]{1,10}', parts[5]), 'STOP_CONTAINER_IDENTITY')
    return tuple(parts)


class NativeDocker:
    """Fixed binary/arguments; bounded pipes and deadline; no error text egress."""
    def __init__(self, expected_hash):
        self.fd = os.open(DOCKER, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(self.fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and
                    info.st_nlink == 1 and not stat.S_IMODE(info.st_mode) & 0o022 and
                    info.st_size <= 256 * 1024 * 1024, 'STOP_DOCKER_BINARY')
            with os.fdopen(os.dup(self.fd), 'rb') as stream:
                digest, count = hashlib.sha256(), 0
                while chunk := stream.read(65536):
                    count += len(chunk)
                    require(count <= 256 * 1024 * 1024, 'STOP_DOCKER_BINARY')
                    digest.update(chunk)
            require(count == info.st_size and digest.hexdigest() == expected_hash, 'STOP_DOCKER_BINARY')
        except BaseException:
            self.close()
            raise

    def close(self):
        os.close(self.fd)

    def __call__(self, args, data, deadline):
        allowed = [PSQL_ARGS, ('inspect', '--type', 'container', '--format', INSPECT_FORMAT, API),
                   ('inspect', '--type', 'container', '--format', INSPECT_FORMAT, POSTGRES)]
        require(tuple(args) in allowed and (data is None or tuple(args) == PSQL_ARGS), 'STOP_TRANSPORT_ARGS')
        require(deadline > time.monotonic(), 'STOP_OBSERVATION_TIMEOUT')
        proc = subprocess.Popen(['/proc/self/fd/' + str(self.fd), *args], executable='/proc/self/fd/' + str(self.fd),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
            pass_fds=(self.fd,), cwd='/', env={'PATH': '/usr/bin:/bin', 'LANG': 'C',
            'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONFIG': '/nonexistent'})
        output = bytearray()
        pending = memoryview(data or b'')
        try:
            with selectors.DefaultSelector() as poll:
                for stream in (proc.stdout, proc.stderr):
                    os.set_blocking(stream.fileno(), False)
                    poll.register(stream, selectors.EVENT_READ)
                if pending:
                    os.set_blocking(proc.stdin.fileno(), False)
                    poll.register(proc.stdin, selectors.EVENT_WRITE)
                else:
                    proc.stdin.close()
                while poll.get_map():
                    remaining = deadline - time.monotonic()
                    require(remaining > 0, 'STOP_OBSERVATION_TIMEOUT')
                    for key, _ in poll.select(min(remaining, 0.1)):
                        stream = key.fileobj
                        if stream is proc.stdin:
                            count = os.write(stream.fileno(), pending[:4096])
                            pending = pending[count:]
                            if not pending:
                                poll.unregister(stream)
                                stream.close()
                        else:
                            chunk = os.read(stream.fileno(), 4096)
                            if not chunk:
                                poll.unregister(stream)
                            elif stream is proc.stderr:
                                raise Stop('STOP_OBSERVATION_READ_FAILED')
                            else:
                                output.extend(chunk)
                                require(len(output) <= MAX_BYTES, 'STOP_OBSERVATION_SIZE')
                require(proc.wait(timeout=max(0.001, deadline-time.monotonic())) == 0,
                        'STOP_OBSERVATION_READ_FAILED')
                return bytes(output)
        except subprocess.TimeoutExpired:
            raise Stop('STOP_OBSERVATION_TIMEOUT') from None
        except (OSError, ValueError):
            raise Stop('STOP_OBSERVATION_READ_FAILED') from None
        finally:
            if proc.poll() is None:
                proc.kill()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def collect(binding, target, operation, schema, run, deadline):
    validate_binding(binding, target, operation)
    identities = {}
    for name, container in (('api', API), ('postgres', POSTGRES)):
        identities[name] = parse_identity(run(('inspect', '--type', 'container', '--format', INSPECT_FORMAT,
                                               container), None, deadline), binding[name])
    snapshot = validate_snapshot(parse_json(run(PSQL_ARGS, build_sql(binding, target, schema), deadline)))
    for name, container in (('api', API), ('postgres', POSTGRES)):
        current = parse_identity(run(('inspect', '--type', 'container', '--format', INSPECT_FORMAT,
                                      container), None, deadline), binding[name])
        require(current == identities[name], 'STOP_IDENTITY_CHANGED_DURING_READ')
    require(deadline >= time.monotonic(), 'STOP_OBSERVATION_TIMEOUT')
    clear = snapshot_clear(snapshot)
    return {'status': 'SNAPSHOT_CLEAR_NOT_ADMISSION' if clear else 'SNAPSHOT_BLOCKED',
            'target': target, 'operation': operation, 'snapshot': snapshot,
            'containerIdentityStable': True, 'runtimeSource': 'NOT_OBSERVED',
            'configPolicyAuthority': 'NOT_OBSERVED', 'operationalExclusion': 'NOT_OBSERVED',
            'nodeTransactions': 'NOT_OBSERVED', 'liveAdmission': False, 'serverWrites': 0}


def read_binding(expected_hash, target, operation):
    require(hex_string(expected_hash, 64), 'STOP_BINDING_HASH')
    info = os.fstat(0)
    require(stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 0 and
            stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1 and
            0 < info.st_size <= MAX_BYTES, 'STOP_BINDING_PRIVATE_INPUT')
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    require(sha256(raw) == expected_hash, 'STOP_BINDING_HASH')
    return validate_binding(parse_json(raw), target, operation)


def load_schema():
    raw = Path(__file__).with_name('xui_owner_central_schema.json').read_bytes()
    require(sha256(raw) == SCHEMA_SHA256, 'STOP_COLLECTOR_SCHEMA_PIN')
    return parse_json(raw)['database']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect-readonly', action='store_true')
    parser.add_argument('--target', choices=TARGETS)
    parser.add_argument('--operation')
    parser.add_argument('--binding-sha256')
    args = parser.parse_args(argv)
    if not args.collect_readonly:
        print('{"status":"INERT_READONLY_ACTION_REQUIRED","serverWrites":0}')
        return 77
    transport = None
    try:
        require(sys.platform == 'linux' and os.geteuid() == 0, 'STOP_NATIVE_LINUX_ROOT_REQUIRED')
        require(args.target in TARGETS and hex_string(args.operation, 32), 'STOP_TARGET_OR_OPERATION')
        binding = read_binding(args.binding_sha256, args.target, args.operation)
        schema = load_schema()
        deadline = time.monotonic() + OUTER_SECONDS
        transport = NativeDocker(binding['dockerSha256'])
        result = collect(binding, args.target, args.operation, schema, transport, deadline)
        print(json.dumps(result, sort_keys=True, separators=(',', ':')))
        return 0 if result['status'] == 'SNAPSHOT_CLEAR_NOT_ADMISSION' else 78
    except (Stop, OSError, ValueError, UnicodeError):
        error = sys.exc_info()[1]
        print(json.dumps({'status': error.code if isinstance(error, Stop) else 'STOP_OBSERVATION_UNKNOWN',
                          'liveAdmission': False, 'nodeTransactions': 'NOT_OBSERVED', 'serverWrites': 0}))
        return 78
    finally:
        if transport is not None:
            transport.close()


if __name__ == '__main__':
    raise SystemExit(main())
