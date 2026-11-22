"""Finite observation of ordinary heartbeats, with retained gaps and one writer.

No transport, PKI workflow, service or credential owner is created here. The
existing Agent supplies its real successful/failed heartbeat cycle. An external
admission and an independent final central readback remain required. A hash
chain detects damage; it does not authenticate who produced the observations.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import stat
import ssl
import subprocess
import sys
import time
from typing import Any

try:
    from heartbeat_observation import heartbeat_node_hash, parse_ingress_receipt
except ImportError:
    from .heartbeat_observation import heartbeat_node_hash, parse_ingress_receipt

MAX_MANIFEST = 16384
MAX_SAMPLE = 16384
HASH = re.compile(r'[a-f0-9]{64}\Z')
UUID = re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z')
MANIFEST_KEYS = frozenset({'schema_version','kind','run_id','scope','node_sha256','not_before','stop_at',
    'human_window_sha256','human_window_stop_at','required_seconds','final_readback_seconds',
    'cadence_seconds','max_gap_seconds','max_clock_drift_seconds','max_samples','max_bytes','bindings'})
BINDING_KEYS = frozenset({'agent_source_sha256','config_sha256','api_producer','ingress_producer',
    'central_reader_sha256','clock_evidence_sha256','central_gates_sha256','deployment_preflight_sha256'})
REASONS = frozenset({'TRANSPORT_FAILED','RECEIPT_MISSING','LOCAL_NOT_READY','SOURCE_CONFIG_DRIFT',
    'CLOCK_UNVERIFIED','CLOCK_DISCONTINUITY','CADENCE_GAP','BOOT_OR_WRITER_CHANGED',
    'SERVER_SCOPE_DRIFT','CENTRAL_TIME_NOT_ADVANCED','CREDENTIAL_CONTINUITY_UNPROVEN','OUTSIDE_WINDOW'})
SAMPLE_KEYS = frozenset({'schema_version','manifest_sha256','sequence','previous_sha256','segment',
    'segment_start_sequence','started_at','at','monotonic_start_ns','monotonic_end_ns','boot_id',
    'writer_sha256','ntp_synchronized','transport_succeeded','request_sent_at','receipt','local',
    'result','reasons','attempt_sha256'})
LOCAL_KEYS = frozenset({'auth_mode','mtls_state','retry_pending','health_healthy','agent_source_sha256','config_sha256'})


class ObservationError(RuntimeError):
    """Fixed codes only; arbitrary input and native errors are never exported."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ObservationError(code)


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def unique(pairs):
    value = {}
    for key,item in pairs:
        require(key not in value,'DUPLICATE_JSON_KEY')
        value[key] = item
    return value


def timestamp(value: Any) -> datetime:
    require(type(value) is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z',value) is not None,'INVALID_TIMESTAMP')
    result = datetime.fromisoformat(value.replace('Z','+00:00'))
    require(result.isoformat(timespec='milliseconds').replace('+00:00','Z') == value,'INVALID_TIMESTAMP')
    return result


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')


def request_time(value: Any) -> bool:
    if value is None:
        return True
    if type(value) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z',value) is None:
        return False
    try:
        datetime.fromisoformat(value.replace('Z','+00:00'))
        return True
    except ValueError:
        return False


def valid_hash(value: Any) -> bool:
    return type(value) is str and HASH.fullmatch(value) is not None


def producer(value: Any) -> bool:
    return (type(value) is dict and set(value)=={'artifact_sha256','instance_sha256'}
        and all(valid_hash(item) for item in value.values()))


def validate_manifest(value: Any) -> dict:
    require(type(value) is dict and set(value)==MANIFEST_KEYS,'RUN_MANIFEST_SCHEMA')
    require(type(value['schema_version']) is int and value['schema_version']==1
        and value['kind']=='MANAGED_MTLS_OBSERVATION_RUN_V1','RUN_MANIFEST_SCHEMA')
    require(type(value['run_id']) is str and re.fullmatch('[a-f0-9]{32}',value['run_id']) is not None,'RUN_ID')
    require(value['scope'] in {'synthetic','installed_node'} and valid_hash(value['node_sha256'])
        and valid_hash(value['human_window_sha256']),'RUN_SCOPE')
    start,end,human_end = (timestamp(value[k]) for k in ('not_before','stop_at','human_window_stop_at'))
    require(start<end<=human_end and (end-start).total_seconds()<=7*86400,'RUN_WINDOW')
    bounds = {'required_seconds':(1,7*86400),'final_readback_seconds':(30,900),'cadence_seconds':(5,300),
        'max_gap_seconds':(5,900),'max_clock_drift_seconds':(0,5),'max_samples':(2,20000),
        'max_bytes':(1048576,128*1048576)}
    for key,(low,high) in bounds.items():
        require(type(value[key]) is int and low<=value[key]<=high,'RUN_BOUND')
    require(value['cadence_seconds']<=value['max_gap_seconds']<=3*value['cadence_seconds'],'RUN_CADENCE')
    require(value['final_readback_seconds']>=value['cadence_seconds']+30,'FINAL_READBACK_RESERVE')
    require(value['required_seconds']+value['cadence_seconds']<=(end-start).total_seconds()
        and value['final_readback_seconds']<=(human_end-end).total_seconds(),'RUN_DOES_NOT_FIT_WINDOW')
    require(value['scope']!='installed_node' or value['required_seconds']>=86400,'SHORT_INTERVAL_IS_SYNTHETIC_ONLY')
    bindings=value['bindings']
    require(type(bindings) is dict and set(bindings)==BINDING_KEYS,'RUN_BINDINGS')
    require(all(producer(bindings[k]) if k.endswith('_producer') else valid_hash(bindings[k])
        for k in bindings),'RUN_BINDINGS')
    return value


def validate_event(event: Any) -> None:
    require(type(event) is dict and set(event)=={'started_at','at','monotonic_start_ns','monotonic_end_ns',
        'boot_id','writer_sha256','ntp_synchronized','transport_succeeded','request_sent_at','receipt','local'},'EVENT_SCHEMA')
    for key in ('started_at','at'):
        timestamp(event[key])
    for key in ('monotonic_start_ns','monotonic_end_ns'):
        require(type(event[key]) is int and event[key]>=0,'EVENT_MONOTONIC')
    require(type(event['boot_id']) is str and UUID.fullmatch(event['boot_id']) is not None
        and valid_hash(event['writer_sha256']),'EVENT_PROCESS_IDENTITY')
    require(type(event['ntp_synchronized']) is bool and type(event['transport_succeeded']) is bool,'EVENT_BOOLEAN')
    require(request_time(event['request_sent_at']),'EVENT_REQUEST_TIME')
    local=event['local']
    require(type(local) is dict and set(local)==LOCAL_KEYS,'EVENT_LOCAL_SCHEMA')
    require(local['auth_mode'] in {'bearer','bootstrap-mtls','mtls',None}
        and local['mtls_state'] in {'BEARER_ONLY','ENROLLING','SHADOW_READY','SHADOW_ACTIVE','ROTATING','FALLBACK','BLOCKED',None},'EVENT_LOCAL_ENUM')
    require(type(local['retry_pending']) is bool and type(local['health_healthy']) is bool
        and all(valid_hash(local[k]) for k in ('agent_source_sha256','config_sha256')),'EVENT_LOCAL_BINDING')
    receipt=event['receipt']
    if receipt is not None:
        import base64
        raw=json.dumps(receipt,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
        require(parse_ingress_receipt(base64.urlsafe_b64encode(raw).rstrip(b'=').decode())==receipt,'EVENT_RECEIPT_SCHEMA')


def make_sample(manifest: dict, manifest_sha: str, event: dict, previous: dict | None,
                previous_sha: str | None,attempt_sha256: str) -> dict:
    """Pure gap/lineage decision. Production events come from ManagedObserver."""
    validate_manifest(manifest)
    validate_event(event)
    require(valid_hash(manifest_sha) and valid_hash(attempt_sha256)
        and (previous_sha is None or valid_hash(previous_sha)),'SAMPLE_CHAIN_BINDING')
    reasons=[]
    now,start=timestamp(event['at']),timestamp(event['started_at'])
    if not timestamp(manifest['not_before'])<=start<=now<=timestamp(manifest['stop_at']):
        reasons.append('OUTSIDE_WINDOW')
    if not event['transport_succeeded']:
        reasons.append('TRANSPORT_FAILED')
    if not event['ntp_synchronized']:
        reasons.append('CLOCK_UNVERIFIED')
    elapsed=(event['monotonic_end_ns']-event['monotonic_start_ns'])/1e9
    if elapsed<0 or abs((now-start).total_seconds()-elapsed)>manifest['max_clock_drift_seconds']:
        reasons.append('CLOCK_DISCONTINUITY')
    local=event['local'];binding=manifest['bindings'];receipt=event['receipt']
    if (local['auth_mode']!='mtls' or local['mtls_state']!='SHADOW_ACTIVE'
            or local['retry_pending'] or not local['health_healthy']):
        reasons.append('LOCAL_NOT_READY')
    if any(local[k]!=binding[k] for k in ('agent_source_sha256','config_sha256')):
        reasons.append('SOURCE_CONFIG_DRIFT')
    if receipt is None:
        reasons.append('RECEIPT_MISSING')
    else:
        try:
            sent=datetime.fromisoformat(event['request_sent_at'].replace('Z','+00:00'))
            server=timestamp(receipt['received_at'])
            drift=manifest['max_clock_drift_seconds']
            if (receipt['node_sha256']!=manifest['node_sha256'] or receipt['api_producer']!=binding['api_producer']
                    or receipt['ingress_producer']!=binding['ingress_producer']
                    or receipt['request_sent_at']!=stamp(sent)
                    or not -drift<=(server-start).total_seconds()
                    or not (server-now).total_seconds()<=drift
                    or timestamp(receipt['credential_expires_at'])<=now):
                reasons.append('SERVER_SCOPE_DRIFT')
        except (ValueError,TypeError,AttributeError):
            reasons.append('SERVER_SCOPE_DRIFT')
    sequence=1 if previous is None else previous['sequence']+1
    segment=1 if previous is None else previous['segment']
    segment_start=sequence if previous is None else previous['segment_start_sequence']
    if previous is not None:
        same_boot=previous['boot_id']==event['boot_id']
        if not same_boot or previous['writer_sha256']!=event['writer_sha256']:
            reasons.append('BOOT_OR_WRITER_CHANGED')
        if same_boot:
            gap=(event['monotonic_end_ns']-previous['monotonic_end_ns'])/1e9
            wall=(now-timestamp(previous['at'])).total_seconds()
            if gap<=0 or gap>manifest['max_gap_seconds']:
                reasons.append('CADENCE_GAP')
            if gap<=0 or abs(wall-gap)>manifest['max_clock_drift_seconds']:
                reasons.append('CLOCK_DISCONTINUITY')
        old=previous.get('receipt')
        if receipt is not None and old is not None:
            if timestamp(receipt['received_at'])<=timestamp(old['received_at']):
                reasons.append('CENTRAL_TIME_NOT_ADVANCED')
            if receipt['credential_sha256']!=old['credential_sha256']:
                acknowledged=timestamp(receipt['credential_acknowledged_at'])
                if (acknowledged<timestamp(old['received_at']) or acknowledged>timestamp(old['credential_expires_at'])
                        or timestamp(receipt['credential_not_before'])>timestamp(old['credential_expires_at'])):
                    reasons.append('CREDENTIAL_CONTINUITY_UNPROVEN')
            elif receipt['certificate_fingerprint_sha256']!=old['certificate_fingerprint_sha256']:
                reasons.append('SERVER_SCOPE_DRIFT')
        if reasons or previous['result']!='QUALIFYING':
            segment+=1
            segment_start=sequence
    return dict(schema_version=1,manifest_sha256=manifest_sha,sequence=sequence,previous_sha256=previous_sha,
        segment=segment,segment_start_sequence=segment_start,attempt_sha256=attempt_sha256,**event,
        result='GAP' if reasons else 'QUALIFYING',reasons=sorted(set(reasons)))


def no_symlinks(path: Path) -> None:
    require(path.is_absolute() and '..' not in path.parts,'UNSAFE_PATH')
    require(all(not p.is_symlink() for p in (path,*path.parents)),'UNSAFE_PATH')


def private_directory(path: Path) -> None:
    no_symlinks(path)
    info=path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and stat.S_IMODE(info.st_mode)==0o700,'UNSAFE_DIRECTORY')


def read_bytes(path: Path, maximum: int, *, private: bool = True) -> bytes:
    no_symlinks(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        s=os.fstat(fd)
        require(stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_nlink==1 and 0<=s.st_size<=maximum
            and not s.st_mode & (0o077 if private else 0o022),'UNSAFE_FILE')
        with os.fdopen(fd,'rb',closefd=False) as stream:
            raw=stream.read(maximum+1)
        after=os.fstat(fd);current=path.lstat()
        require(len(raw)==s.st_size and len(raw)<=maximum
            and (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
            and (s.st_dev,s.st_ino)==(current.st_dev,current.st_ino),'FILE_CHANGED')
        return raw
    finally:
        os.close(fd)


def read_json(path: Path, maximum: int = MAX_SAMPLE) -> tuple[dict,bytes]:
    raw=read_bytes(path,maximum)
    value=json.loads(raw,object_pairs_hook=unique,
        parse_constant=lambda _v: (_ for _ in ()).throw(ObservationError('INVALID_JSON')))
    require(type(value) is dict,'INVALID_JSON')
    return value,raw


def fsync_directory(path: Path) -> None:
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_run(path: Path) -> tuple[dict,str,dict]:
    require(os.name=='posix' and os.geteuid()==0,'POSIX_ROOT_REQUIRED')
    private_directory(path.parent)
    manifest,raw=read_json(path,MAX_MANIFEST)
    validate_manifest(manifest)
    require(path.name=='manifest.json' and path.parent.name==manifest['run_id'],'FIXED_RUN_NAMESPACE')
    require(path.parent.parent==Path('/var/lib/wavemesh-agent/managed-observation'),'FIXED_RUN_NAMESPACE')
    sha=hashlib.sha256(raw).hexdigest()
    admission,_=read_json(path.parent/'admission.json',MAX_MANIFEST)
    require(set(admission)=={'kind','approved','sourceProposal','manifest_sha256','human_window_sha256',
        'node_sha256','scope','stop_at','clock_boot_id','clock_evidence_sha256','deployment_preflight_sha256'},'ADMISSION_SCHEMA')
    require(admission['kind']=='ROOT_MANAGED_MTLS_OBSERVATION_ADMISSION' and admission['approved'] is True
        and admission['sourceProposal'] is False,'ACTUAL_ADMISSION_REQUIRED')
    require(all(admission[k]==manifest[k] for k in ('human_window_sha256','node_sha256','scope','stop_at'))
        and admission['manifest_sha256']==sha and admission['clock_evidence_sha256']==manifest['bindings']['clock_evidence_sha256']
        and admission['deployment_preflight_sha256']==manifest['bindings']['deployment_preflight_sha256']
        and type(admission['clock_boot_id']) is str and UUID.fullmatch(admission['clock_boot_id']) is not None,'ADMISSION_BINDING')
    return manifest,sha,admission


class RunJournal:
    """POSIX-only exclusive writer; uncertain writes stop without repair/retry."""
    def __init__(self,path: Path,manifest: dict,manifest_sha: str):
        import fcntl
        self.path=path;self.manifest=manifest;self.manifest_sha=manifest_sha
        self.lock=None;self.samples=[];self.tip=None;self.bytes=0;self.uncertain=False;self.pending_attempt=None
        private_directory(path)
        self.directory_identity=(path.stat().st_dev,path.stat().st_ino)
        lock=path/'writer.lock'
        if not lock.exists():
            fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            try:os.fsync(fd)
            finally:os.close(fd)
            fsync_directory(path)
        read_bytes(lock,0)
        fd=os.open(lock,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            s=os.fstat(fd);current=lock.lstat()
            require((s.st_dev,s.st_ino)==(current.st_dev,current.st_ino),'LOCK_REPLACED')
            self.lock_identity=(s.st_dev,s.st_ino)
            self.lock=fd
            self.samples,self.tip,self.bytes=read_history(path,manifest,manifest_sha)
            require(not any((path/name).exists() for name in ('STOPPED.json','BLOCKED.json')),'RETAINED_TERMINAL_RUN_NO_RESUME')
        except BaseException:
            os.close(fd);self.lock=None
            raise

    def verify_namespace(self,*,allow_uncertain: bool = False) -> None:
        require(self.lock is not None and (allow_uncertain or not self.uncertain),'JOURNAL_BLOCKED')
        private_directory(self.path)
        current=self.path.stat();lock=(self.path/'writer.lock').lstat();held=os.fstat(self.lock)
        require((current.st_dev,current.st_ino)==self.directory_identity
            and (lock.st_dev,lock.st_ino)==(held.st_dev,held.st_ino)==self.lock_identity,'JOURNAL_OR_LOCK_REPLACED')

    def begin_attempt(self,request_sent_at: str | None) -> None:
        self.verify_namespace()
        require(self.pending_attempt is None,'ORIGINAL_ATTEMPT_UNRESOLVED')
        require(request_time(request_sent_at),'ATTEMPT_REQUEST_TIME')
        require(len(self.samples)<self.manifest['max_samples']
            and self.bytes+2*MAX_SAMPLE<=self.manifest['max_bytes']-2*MAX_SAMPLE,'JOURNAL_LIMIT')
        sequence=len(self.samples)+1
        value=dict(kind='MANAGED_OBSERVATION_ORIGINAL_ATTEMPT',manifest_sha256=self.manifest_sha,
            sequence=sequence,previous_sha256=self.tip,request_sent_at=request_sent_at,
            at=stamp(datetime.now(timezone.utc)))
        raw=encoded(value)+b'\n';path=self.path/('attempt-'+str(sequence).zfill(8)+'.json')
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as stream:
                require(stream.write(raw)==len(raw),'ATTEMPT_SHORT_WRITE');stream.flush();os.fsync(stream.fileno())
            fsync_directory(self.path)
            require(read_bytes(path,MAX_SAMPLE)==raw,'ATTEMPT_POSTIMAGE_CHANGED')
        except BaseException:
            self.uncertain=True
            raise
        self.pending_attempt=dict(value=value,sha256=hashlib.sha256(raw).hexdigest())
        self.bytes+=len(raw)

    def append(self,event: dict) -> dict:
        self.verify_namespace()
        require(self.pending_attempt is not None,'ORIGINAL_ATTEMPT_REQUIRED')
        require(event['request_sent_at']==self.pending_attempt['value']['request_sent_at'],'ACTUAL_REQUEST_CHANGED')
        previous=self.samples[-1] if self.samples else None
        sample=make_sample(self.manifest,self.manifest_sha,event,previous,self.tip,self.pending_attempt['sha256'])
        raw=encoded(sample)+b'\n'
        require(len(raw)<=MAX_SAMPLE and len(self.samples)<self.manifest['max_samples']
            and self.bytes+len(raw)<=self.manifest['max_bytes']-2*MAX_SAMPLE,'JOURNAL_LIMIT')
        target=self.path/(str(sample['sequence']).zfill(8)+'.json')
        # No temporary replacement: partial/corrupt/ambiguous evidence remains.
        try:
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as stream:
                require(stream.write(raw)==len(raw),'SAMPLE_SHORT_WRITE')
                stream.flush();os.fsync(stream.fileno())
            fsync_directory(self.path)
            require(read_bytes(target,MAX_SAMPLE)==raw,'SAMPLE_POSTIMAGE_CHANGED')
        except BaseException:
            self.uncertain=True
            raise
        self.samples.append(sample);self.tip=hashlib.sha256(raw).hexdigest();self.bytes+=len(raw)
        self.pending_attempt=None
        return sample

    def terminal(self,reason: str,*,stopped: bool = False) -> None:
        self.verify_namespace(allow_uncertain=True)
        require(datetime.now(timezone.utc)<timestamp(self.manifest['human_window_stop_at']),'HUMAN_WINDOW_EXPIRED_NO_WRITE')
        require(reason in {'DEADLINE','LOCAL_PROBE_FAILED','JOURNAL_UNCERTAIN','CLOCK_UNVERIFIED',
            'BINDING_DRIFT','INTERRUPTED'},'TERMINAL_REASON')
        name='STOPPED.json' if stopped else 'BLOCKED.json'
        raw=encoded(dict(kind='MANAGED_OBSERVATION_STOPPED' if stopped else 'MANAGED_OBSERVATION_BLOCKED',
            manifest_sha256=self.manifest_sha,reason=reason,at=stamp(datetime.now(timezone.utc)),
            last_sequence=len(self.samples),tip_sha256=self.tip))+b'\n'
        fd=os.open(self.path/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:
            require(stream.write(raw)==len(raw),'TERMINAL_SHORT_WRITE');stream.flush();os.fsync(stream.fileno())
        fsync_directory(self.path)

    def close(self) -> None:
        if self.lock is not None:
            fd,self.lock=self.lock,None
            os.close(fd)


def read_history(path: Path,manifest: dict,manifest_sha: str) -> tuple[list[dict],str|None,int]:
    private_directory(path)
    all_names=sorted(p.name for p in path.iterdir())
    controls={'STOPPED.json','BLOCKED.json'}&set(all_names)
    names=[n for n in all_names if n not in controls and n!='writer.lock']
    require('writer.lock' in all_names and len(names)%2==0 and len(names)<=2*manifest['max_samples'],'HISTORY_UNRESOLVED_ATTEMPT_OR_LIMIT')
    count=len(names)//2
    require(set(names)=={str(n).zfill(8)+'.json' for n in range(1,count+1)}
        |{'attempt-'+str(n).zfill(8)+'.json' for n in range(1,count+1)},'HISTORY_SEQUENCE_OR_FOREIGN_FILE')
    samples=[];tip=None;used=0
    for sequence in range(1,count+1):
        name=str(sequence).zfill(8)+'.json'
        attempt,attempt_raw=read_json(path/('attempt-'+name))
        require(set(attempt)=={'kind','manifest_sha256','sequence','previous_sha256','request_sent_at','at'}
            and attempt['kind']=='MANAGED_OBSERVATION_ORIGINAL_ATTEMPT' and attempt['manifest_sha256']==manifest_sha
            and attempt['sequence']==sequence and attempt['previous_sha256']==tip
            and attempt_raw==encoded(attempt)+b'\n','ORIGINAL_ATTEMPT_BINDING')
        timestamp(attempt['at'])
        value,raw=read_json(path/name)
        require(set(value)==SAMPLE_KEYS and raw==encoded(value)+b'\n','SAMPLE_SCHEMA_OR_BYTES')
        event={k:value[k] for k in ('started_at','at','monotonic_start_ns','monotonic_end_ns','boot_id',
            'writer_sha256','ntp_synchronized','transport_succeeded','request_sent_at','receipt','local')}
        require(value['request_sent_at']==attempt['request_sent_at'],'ORIGINAL_ATTEMPT_REQUEST_BINDING')
        expected=make_sample(manifest,manifest_sha,event,samples[-1] if samples else None,tip,hashlib.sha256(attempt_raw).hexdigest())
        require(value==expected,'SAMPLE_LINEAGE_OR_DECISION')
        used+=len(raw)+len(attempt_raw);require(used<=manifest['max_bytes'],'HISTORY_LIMIT')
        samples.append(value);tip=hashlib.sha256(raw).hexdigest()
    for name in controls:
        control,raw=read_json(path/name)
        require(set(control)=={'kind','manifest_sha256','reason','at','last_sequence','tip_sha256'}
            and control['kind']==('MANAGED_OBSERVATION_STOPPED' if name=='STOPPED.json' else 'MANAGED_OBSERVATION_BLOCKED')
            and control['manifest_sha256']==manifest_sha and raw==encoded(control)+b'\n','TERMINAL_SCHEMA')
        timestamp(control['at'])
        require(control['reason'] in {'DEADLINE','LOCAL_PROBE_FAILED','JOURNAL_UNCERTAIN','CLOCK_UNVERIFIED','BINDING_DRIFT','INTERRUPTED'},'TERMINAL_REASON')
        # A failed/partial append may leave later bytes; never silently discard it.
        require(control['last_sequence']==len(samples) and control['tip_sha256']==tip,'TERMINAL_HISTORY_BINDING')
        used+=len(raw);require(used<=manifest['max_bytes'],'HISTORY_LIMIT')
    return samples,tip,used


def source_bundle_sha256() -> str:
    """Public installed Agent sources and runtime dependencies, never state/key files."""
    directory=Path(__file__).absolute().parent
    no_symlinks(directory)
    files=sorted(directory.glob('*.py'))
    require(1<=len(files)<=64,'SOURCE_BUNDLE_LIMIT')
    hashes={p.name:hashlib.sha256(read_bytes(p,2*1048576,private=False)).hexdigest() for p in files}
    openssl=shutil.which('openssl');require(openssl is not None,'DEPENDENCY_BINDING_UNAVAILABLE')
    binaries={'python':Path(sys.executable),'systemctl':Path('/usr/bin/systemctl'),
        'timedatectl':Path('/usr/bin/timedatectl'),'openssl':Path(openssl),'ssl_extension':Path(ssl._ssl.__file__)}
    dependencies={name:hashlib.sha256(read_bytes(path.resolve(),32*1048576,private=False)).hexdigest()
        for name,path in binaries.items()}
    return digest(dict(files=hashes,python=platform.python_version(),kernel=platform.release(),
        libc=list(platform.libc_ver()),openssl=ssl.OPENSSL_VERSION,binaries=dependencies))


def config_sha256(config: Any) -> str:
    # No token/secret/PEM/environment dictionary is retained or serialized.
    fields=('auth_mode','mtls_mode','mtls_environment','heartbeat_seconds','observation_seconds',
        'observed_version','command_mode','runtime_finding_mode','public_profiles_enabled',
        'mtls_rotate_before_seconds','mtls_retry_base_seconds','mtls_retry_max_seconds','mtls_retry_max_attempts')
    value={key:getattr(config,key) for key in fields}
    value['node_sha256']=heartbeat_node_hash(config.tenant_id,config.node_id)
    value['api_base_sha256']=hashlib.sha256(str(config.mtls_api_base).encode()).hexdigest()
    ca=config.mtls_server_ca_file or ssl.get_default_verify_paths().cafile
    require(ca is not None,'CA_BINDING_UNAVAILABLE')
    value['server_ca_sha256']=hashlib.sha256(read_bytes(Path(ca),2*1048576,private=False)).hexdigest()
    value['node_config_sha256']=hashlib.sha256(read_bytes(Path('/etc/wavemesh-node/config.json'),2*1048576,private=False)).hexdigest()
    unit=Path('/etc/systemd/system/wavemesh-node-agent.service')
    value['service_unit_sha256']=hashlib.sha256(read_bytes(unit,65536,private=False)).hexdigest()
    return digest(value)


def clock_and_service(*,current_agent: bool = True) -> dict:
    """Concrete bounded local probes, not caller-supplied PASS metadata."""
    require(os.name=='posix' and os.geteuid()==0,'POSIX_ROOT_REQUIRED')
    with Path('/proc/sys/kernel/random/boot_id').open('rb') as stream:
        boot=stream.read(65).decode('ascii').strip()
    require(UUID.fullmatch(boot) is not None,'BOOT_ID_UNAVAILABLE')
    ntp=subprocess.run(['/usr/bin/timedatectl','show','--property=NTPSynchronized','--value'],
        capture_output=True,timeout=3,check=False)
    service=subprocess.run(['/usr/bin/systemctl','show','wavemesh-node-agent.service',
        '--property=ActiveState','--property=MainPID','--property=NeedDaemonReload','--property=DropInPaths'],
        capture_output=True,timeout=3,check=False)
    require(len(ntp.stdout)<=32 and len(service.stdout)<=4096,'LOCAL_PROBE_SIZE')
    rows={}
    for line in service.stdout.decode('ascii').splitlines():
        key,separator,value=line.partition('=')
        require(separator=='=' and key not in rows,'LOCAL_SERVICE_PROBE')
        rows[key]=value
    service_pid=int(rows.get('MainPID','0'))
    service_ok=(service.returncode==0 and set(rows)=={'ActiveState','MainPID','NeedDaemonReload','DropInPaths'}
        and rows['ActiveState']=='active' and service_pid>1 and (not current_agent or service_pid==os.getpid())
        and rows['NeedDaemonReload']=='no' and rows['DropInPaths']=='')
    with Path('/proc/'+str(service_pid)+'/stat').open('rb') as stream:
        raw=stream.read(4097)
    require(len(raw)<=4096,'LOCAL_PROCESS_PROBE')
    ticks=raw.decode().rsplit(')',1)[1].split()[19]
    require(ticks.isdigit(),'LOCAL_PROCESS_PROBE')
    return dict(boot_id=boot,writer_sha256=digest([boot,service_pid,ticks]),
        ntp_synchronized=ntp.returncode==0 and ntp.stdout.strip()==b'yes',service_ok=service_ok)


class ManagedObserver:
    """Optional finite journal owned by the existing long-running Node Agent."""
    def __init__(self,path: Path,config: Any):
        self.path=path;self.config=config;self.journal=None;self.status='STARTING'
        self.manifest,self.manifest_sha,self.admission=load_run(path)
        self.admission_sha=hashlib.sha256(read_bytes(path.parent/'admission.json',MAX_MANIFEST)).hexdigest()
        require(self.manifest['node_sha256']==heartbeat_node_hash(config.tenant_id,config.node_id)
            and self.manifest['cadence_seconds']==config.heartbeat_seconds,'AGENT_RUN_BINDING')
        require(self.manifest['final_readback_seconds']>=config.heartbeat_seconds+config.request_timeout_seconds+36,'FINAL_PROBE_RESERVE')
        now=datetime.now(timezone.utc)
        require(timestamp(self.manifest['not_before'])<=now<timestamp(self.manifest['stop_at']),'RUN_NOT_ACTIVE')
        history=path.parent/'history'
        if not history.exists():
            history.mkdir(mode=0o700)
            fsync_directory(path.parent)
        self.journal=RunJournal(history,self.manifest,self.manifest_sha)
        try:
            if not self.journal.samples:
                require((timestamp(self.manifest['stop_at'])-now).total_seconds()>=
                    self.manifest['required_seconds']+config.heartbeat_seconds+config.request_timeout_seconds+6,'ACTUAL_WINDOW_TOO_SHORT')
            self.status='ACTIVE'
        except BaseException:
            self.close()
            raise

    def begin(self,request_sent_at: str | None = None) -> dict | None:
        if self.status!='ACTIVE':
            return None
        try:
            now=datetime.now(timezone.utc)
            if now>=timestamp(self.manifest['human_window_stop_at']):
                self.status='EXPIRED_WITHOUT_TERMINAL_PROOF';self.close();return None
            require(hashlib.sha256(read_bytes(self.path,MAX_MANIFEST)).hexdigest()==self.manifest_sha
                and hashlib.sha256(read_bytes(self.path.parent/'admission.json',MAX_MANIFEST)).hexdigest()==self.admission_sha,
                'FROZEN_RUN_BINDING_CHANGED')
            self.journal.begin_attempt(request_sent_at)
            observed=clock_and_service()
            # A reboot loses the admitted clock binding; it cannot qualify itself.
            observed['ntp_synchronized']=(observed['ntp_synchronized']
                and observed['boot_id']==self.admission['clock_boot_id'])
            observed.update(agent_source_sha256=source_bundle_sha256(),config_sha256=config_sha256(self.config))
            observed.update(started_at=stamp(datetime.now(timezone.utc)),monotonic_start_ns=time.monotonic_ns())
            observed['final_readback']=now>=timestamp(self.manifest['stop_at'])
            return observed
        except Exception:
            self.block('LOCAL_PROBE_FAILED');return None

    def finish(self,attempt: dict,*,transport_succeeded: bool,request_sent_at: str | None,
               receipt: dict | None,mtls_status: dict,health: dict) -> None:
        if self.status!='ACTIVE':
            return
        try:
            now=datetime.now(timezone.utc);end=time.monotonic_ns()
            if now>=timestamp(self.manifest['human_window_stop_at']):
                self.status='EXPIRED_WITHOUT_TERMINAL_PROOF';self.close();return
            first=self.journal.samples[0] if self.journal.samples else None
            if (first is not None
                    and first['boot_id']==attempt['boot_id']
                    and (end-first['monotonic_start_ns'])/1e9>
                        (timestamp(self.manifest['human_window_stop_at'])-timestamp(self.manifest['not_before'])).total_seconds()):
                self.block('CLOCK_UNVERIFIED');return
            mode=mtls_status.get('state')
            local=dict(auth_mode=self.config.auth_mode,mtls_state=mode if mode in
                {'BEARER_ONLY','ENROLLING','SHADOW_READY','SHADOW_ACTIVE','ROTATING','FALLBACK','BLOCKED'} else None,
                retry_pending=(mtls_status.get('retry_attempts')!=0 or mtls_status.get('retry_at') is not None
                    or mtls_status.get('code') is not None),
                health_healthy=health.get('node_status')=='healthy' and attempt['service_ok'],
                agent_source_sha256=attempt['agent_source_sha256'],config_sha256=attempt['config_sha256'])
            event=dict(started_at=attempt['started_at'],at=stamp(now),monotonic_start_ns=attempt['monotonic_start_ns'],
                monotonic_end_ns=end,boot_id=attempt['boot_id'],writer_sha256=attempt['writer_sha256'],
                ntp_synchronized=attempt['ntp_synchronized'],transport_succeeded=transport_succeeded,
                request_sent_at=request_sent_at,receipt=receipt,local=local)
            sample=self.journal.append(event)
            if not attempt['ntp_synchronized']:
                self.block('CLOCK_UNVERIFIED')
            elif any(local[k]!=self.manifest['bindings'][k] for k in ('agent_source_sha256','config_sha256')):
                self.block('BINDING_DRIFT')
            elif attempt['final_readback'] or now>=timestamp(self.manifest['stop_at']):
                if set(sample['reasons'])<= {'OUTSIDE_WINDOW'}:
                    self.journal.terminal('DEADLINE',stopped=True)
                    self.status='STOPPED_AT_DEADLINE';self.close()
                else:
                    self.block('LOCAL_PROBE_FAILED')
        except Exception:
            self.block('JOURNAL_UNCERTAIN')

    def block(self,reason: str) -> None:
        self.status='BLOCKED_RETAINED'
        try:
            if datetime.now(timezone.utc)>=timestamp(self.manifest['human_window_stop_at']):
                self.status='EXPIRED_WITHOUT_TERMINAL_PROOF'
                return
            if self.journal is not None:
                self.journal.terminal(reason)
        except Exception:
            self.status='BLOCKED_TERMINAL_DURABILITY_UNKNOWN'
        finally:
            self.close()

    def interrupt(self) -> None:
        if self.status=='ACTIVE':
            self.block('INTERRUPTED')
        else:
            self.close()

    def close(self) -> None:
        if self.journal is not None:
            self.journal.close()


def last_interval(manifest: dict,samples: list[dict]) -> tuple[list[dict],dict]:
    require(len(samples)>=2,'INTERVAL_TOO_SHORT')
    anchor=samples[-1]
    if anchor['result']=='GAP':
        require(anchor['reasons']==['OUTSIDE_WINDOW'],'FINAL_OBSERVATION_FAILED')
        end=len(samples)-2
    else:
        end=len(samples)-1
    require(samples[end]['result']=='QUALIFYING','NO_QUALIFYING_ENDPOINT')
    start=end
    while start>0 and samples[start-1]['result']=='QUALIFYING' and samples[start-1]['segment']==samples[end]['segment']:
        start-=1
    interval=samples[start:end+1]
    require(len(interval)>=2 and interval[0]['sequence']==interval[0]['segment_start_sequence'],'INTERVAL_LINEAGE')
    require(all(row['boot_id']==interval[0]['boot_id'] and row['writer_sha256']==interval[0]['writer_sha256']
        for row in interval),'INTERVAL_BOOT_OR_PROCESS')
    elapsed=(interval[-1]['monotonic_end_ns']-interval[0]['monotonic_end_ns'])/1e9
    wall=(timestamp(interval[-1]['at'])-timestamp(interval[0]['at'])).total_seconds()
    require(elapsed>=manifest['required_seconds'] and abs(wall-elapsed)<=manifest['max_clock_drift_seconds'],'INTERVAL_DURATION_OR_CLOCK')
    return interval,anchor


def qualify_interval(manifest: dict,manifest_sha: str,samples: list[dict],central: dict,local: dict,
                     now: datetime) -> dict:
    """Contracts over concrete file/SQL/local readers, not a PASS-input adapter."""
    validate_manifest(manifest)
    interval,anchor=last_interval(manifest,samples)
    require(now.tzinfo is not None and now<timestamp(manifest['human_window_stop_at']),'FINAL_WINDOW_EXPIRED')
    require(0<=(now-timestamp(anchor['at'])).total_seconds()<=manifest['max_gap_seconds'],'FINAL_SAMPLE_STALE')
    receipt=anchor['receipt'];require(receipt is not None,'FINAL_RECEIPT_REQUIRED')
    keys={'schema_version','kind','manifest_sha256','deployment_preflight_sha256','node_sha256','observed_at','database_at',
        'reader_artifact_sha256','configured_api_producer','effective_gates_sha256','transaction_read_only','source_provenance',
        'last_heartbeat_at','active_credential_sha256','active_certificate_fingerprint_sha256','node_active','node_status',
        'reconciliation_status','desired_version','observed_version','unresolved_lifecycle','credentials'}
    require(type(central) is dict and set(central)==keys and type(central['schema_version']) is int and central['schema_version']==1
        and central['kind']=='MANAGED_MTLS_OBSERVATION_CENTRAL_READBACK_V1','CENTRAL_READER_SCHEMA')
    bindings=manifest['bindings']
    require(central['manifest_sha256']==manifest_sha and central['node_sha256']==manifest['node_sha256']
        and central['deployment_preflight_sha256']==bindings['deployment_preflight_sha256']
        and central['reader_artifact_sha256']==bindings['central_reader_sha256']
        and central['effective_gates_sha256']==bindings['central_gates_sha256']
        and central['configured_api_producer']==bindings['api_producer']
        and central['source_provenance']=='CONFIGURED_BINDING_REQUIRES_EXECUTOR_ADMISSION','CENTRAL_SOURCE_BINDING')
    require(central['transaction_read_only'] is True and central['node_active'] is True
        and central['node_status']=='ACTIVE' and central['reconciliation_status']=='IN_SYNC'
        and central['unresolved_lifecycle'] is False and type(central['desired_version']) is int
        and type(central['observed_version']) is int and central['desired_version']==central['observed_version'],'CENTRAL_CURRENT_HEALTH')
    observed=timestamp(central['observed_at']);database=timestamp(central['database_at'])
    require(timestamp(anchor['at'])<=observed<=now and (now-observed).total_seconds()<=30
        and abs((database-observed).total_seconds())<=5,'CENTRAL_READBACK_TIME')
    require(central['last_heartbeat_at']==receipt['received_at']
        and central['active_credential_sha256']==receipt['credential_sha256']
        and central['active_certificate_fingerprint_sha256']==receipt['certificate_fingerprint_sha256'],'EXACT_CENTRAL_HEARTBEAT_CORRELATION')
    local_keys={'kind','observed_at','node_sha256','agent_source_sha256','config_sha256','writer_sha256','boot_id',
        'ntp_synchronized','service_active','primary_mtls','mtls_state','retry_pending','pending_acknowledgement','certificate_fingerprint_sha256'}
    require(type(local) is dict and set(local)==local_keys and local['kind']=='MANAGED_MTLS_OBSERVATION_LOCAL_READBACK_V1','LOCAL_READER_SCHEMA')
    require(0<=(now-timestamp(local['observed_at'])).total_seconds()<=30 and local['node_sha256']==manifest['node_sha256']
        and all(local[k]==bindings[k] for k in ('agent_source_sha256','config_sha256'))
        and local['writer_sha256']==anchor['writer_sha256'] and local['boot_id']==anchor['boot_id']
        and local['ntp_synchronized'] is True and local['service_active'] is True and local['primary_mtls'] is True
        and local['mtls_state']=='SHADOW_ACTIVE' and local['retry_pending'] is False and local['pending_acknowledgement'] is False
        and local['certificate_fingerprint_sha256']==receipt['certificate_fingerprint_sha256'],'LOCAL_CURRENT_BINDING_OR_HEALTH')
    rows=central['credentials']
    credential_keys={'credential_sha256','certificate_fingerprint_sha256','not_before','expires_at','acknowledged_at',
        'revoked_at','issuer_revoked_at','previous_valid_until','status','replacement_of_sha256'}
    require(type(rows) is list and 1<=len(rows)<=96,'CENTRAL_CREDENTIAL_LIMIT')
    credentials={}
    for row in rows:
        require(type(row) is dict and set(row)==credential_keys and valid_hash(row['credential_sha256'])
            and valid_hash(row['certificate_fingerprint_sha256']) and row['credential_sha256'] not in credentials
            and row['status'] in {'ACTIVE','EXPIRED','REVOKED'}
            and (row['replacement_of_sha256'] is None or valid_hash(row['replacement_of_sha256'])),'CENTRAL_CREDENTIAL_SCHEMA')
        for key in ('not_before','expires_at','acknowledged_at','revoked_at','issuer_revoked_at','previous_valid_until'):
            if row[key] is not None:timestamp(row[key])
        credentials[row['credential_sha256']]=row
    previous=None
    for sample in [*interval,anchor]:
        r=sample['receipt'];row=credentials.get(r['credential_sha256'])
        require(row is not None and row['certificate_fingerprint_sha256']==r['certificate_fingerprint_sha256']
            and row['not_before']==r['credential_not_before'] and row['expires_at']==r['credential_expires_at']
            and row['acknowledged_at']==r['credential_acknowledged_at'],'CREDENTIAL_RECEIPT_LINEAGE')
        received=timestamp(r['received_at'])
        require(timestamp(row['not_before'])<=received<timestamp(row['expires_at']) and timestamp(row['acknowledged_at'])<=received
            and all(row[k] is None or timestamp(row[k])>received for k in ('revoked_at','issuer_revoked_at','previous_valid_until')),
            'CREDENTIAL_NOT_VALID_AT_SAMPLE')
        if previous is not None and previous!=r['credential_sha256']:
            require(row['replacement_of_sha256']==previous,'EXACT_ROTATION_LINEAGE_REQUIRED')
        previous=r['credential_sha256']
    elapsed=(interval[-1]['monotonic_end_ns']-interval[0]['monotonic_end_ns'])/1e9
    return dict(kind='MANAGED_MTLS_OBSERVATION_QUALIFICATION_V1',
        result='PASS_SYNTHETIC_MECHANISM_ONLY' if manifest['scope']=='synthetic' else 'PASS_INSTALLED_MTLS_OBSERVATION_CONTRACT',
        scope=manifest['scope'],qualified_seconds=elapsed,segment=interval[0]['segment'],
        first_sequence=interval[0]['sequence'],last_sequence=interval[-1]['sequence'],samples=len(interval),
        retained_gap_samples=sum(s['result']=='GAP' for s in samples),
        measured_24h=manifest['scope']=='installed_node' and elapsed>=86400,
        executor_and_deployment_provenance='REQUIRES_ADMITTED_INDEPENDENT_EXECUTION_CUSTODY',
        physical_vpn_accepted=False,fleet_accepted=False,full_beta_accepted=False)


def independent_local_readback(config: Any) -> dict:
    """No state owner/lifecycle or private-key read is instantiated by this probe."""
    from types import SimpleNamespace
    try:
        from node_mtls_client import _observation_certificate_fingerprint
    except ImportError:
        from .node_mtls_client import _observation_certificate_fingerprint
    clock=clock_and_service(current_agent=False)
    root=config.mtls_state_root
    private_directory(root);private_directory(root/'generations')
    active=root/'active';info=active.lstat()
    require(stat.S_ISLNK(info.st_mode) and info.st_uid==0,'ACTIVE_SELECTOR_READBACK')
    target=(root/os.readlink(active)).resolve()
    require(target.parent==(root/'generations').resolve() and re.fullmatch('[a-f0-9]{24}',target.name) is not None,'ACTIVE_SELECTOR_READBACK')
    private_directory(target)
    # Match the existing generation's public certificate name; no key/CSR/PEM export.
    fingerprint=_observation_certificate_fingerprint(SimpleNamespace(certificate=target/'client.crt'))
    runtime,_=read_json(root/'runtime.json')
    state=runtime.get('state')
    retry=(type(runtime.get('retry_attempts')) is not int or runtime['retry_attempts']!=0
        or runtime.get('retry_at') is not None or runtime.get('code') is not None)
    return dict(kind='MANAGED_MTLS_OBSERVATION_LOCAL_READBACK_V1',observed_at=stamp(datetime.now(timezone.utc)),
        node_sha256=heartbeat_node_hash(config.tenant_id,config.node_id),agent_source_sha256=source_bundle_sha256(),
        config_sha256=config_sha256(config),writer_sha256=clock['writer_sha256'],boot_id=clock['boot_id'],
        ntp_synchronized=clock['ntp_synchronized'],service_active=clock['service_ok'],primary_mtls=config.auth_mode=='mtls',
        mtls_state=state if state in {'BEARER_ONLY','ENROLLING','SHADOW_READY','SHADOW_ACTIVE','ROTATING','FALLBACK','BLOCKED'} else None,
        retry_pending=retry,pending_acknowledgement=os.path.lexists(root/'acknowledgement.pending.json'),
        certificate_fingerprint_sha256=fingerprint)


def stopped_history(path: Path) -> tuple[dict,str,dict,list[dict],str]:
    import fcntl
    manifest,manifest_sha,admission=load_run(path)
    history=path.parent/'history';private_directory(history)
    lock=history/'writer.lock';read_bytes(lock,0)
    fd=os.open(lock,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
        held=os.fstat(fd);current=lock.lstat()
        require((held.st_dev,held.st_ino)==(current.st_dev,current.st_ino),'LOCK_REPLACED')
        names=sorted(p.name for p in history.iterdir())
        require('STOPPED.json' in names and 'BLOCKED.json' not in names,'FINITE_STOP_NOT_PROVEN')
        samples,tip,_=read_history(history,manifest,manifest_sha)
        stopped,_=read_json(history/'STOPPED.json')
        require(stopped['reason']=='DEADLINE' and timestamp(manifest['stop_at'])<=timestamp(stopped['at'])
            <timestamp(manifest['human_window_stop_at']),'FINITE_STOP_WINDOW')
        require(names==sorted(p.name for p in history.iterdir()) and tip is not None,'HISTORY_CHANGED_DURING_READBACK')
        return manifest,manifest_sha,admission,samples,tip
    finally:
        os.close(fd)


def central_query(manifest: dict,manifest_sha: str,samples: list[dict],config: Any) -> dict:
    interval,anchor=last_interval(manifest,samples)
    require(heartbeat_node_hash(config.tenant_id,config.node_id)==manifest['node_sha256'],'QUERY_NODE_BINDING')
    receipt=anchor['receipt'];require(receipt is not None,'QUERY_FINAL_RECEIPT')
    credentials=sorted({s['receipt']['credential_sha256'] for s in [*interval,anchor]})
    require(len(credentials)<=96,'QUERY_CREDENTIAL_LIMIT')
    return dict(schema_version=1,manifest_sha256=manifest_sha,deployment_preflight_sha256=manifest['bindings']['deployment_preflight_sha256'],
        tenant_id=config.tenant_id,node_id=config.node_id,credential_digests=credentials,last_heartbeat_at=receipt['received_at'],
        last_credential_sha256=receipt['credential_sha256'],last_certificate_fingerprint_sha256=receipt['certificate_fingerprint_sha256'])


def main() -> int:
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare-central-query','qualify'])
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,default=Path('/etc/wavemesh-agent/agent.env'))
    parser.add_argument('--central-readback',type=Path)
    args=parser.parse_args()
    try:
        from node_agent import AgentConfig
        manifest,manifest_sha,admission,samples,tip=stopped_history(args.manifest)
        config=AgentConfig.load(args.env_file)
        require(config.managed_observation_manifest==args.manifest,'CONFIGURED_MANIFEST_CHANGED')
        if args.command=='prepare-central-query':
            require(datetime.now(timezone.utc)<timestamp(manifest['human_window_stop_at']),'HUMAN_WINDOW_EXPIRED_NO_WRITE')
            value=central_query(manifest,manifest_sha,samples,config)
            path=args.manifest.parent/('central-query-'+tip[:16]+'.json');raw=encoded(value)+b'\n'
            if path.exists():require(read_bytes(path,MAX_MANIFEST)==raw,'IMMUTABLE_QUERY_CHANGED')
            else:
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'wb') as stream:
                    require(stream.write(raw)==len(raw),'QUERY_SHORT_WRITE');stream.flush();os.fsync(stream.fileno())
                fsync_directory(path.parent)
            print(json.dumps(dict(kind='MANAGED_CENTRAL_QUERY_READY',path=str(path),sha256=hashlib.sha256(raw).hexdigest())))
        else:
            require(args.central_readback is not None and args.central_readback.parent==args.manifest.parent
                and args.central_readback.name=='central-readback-'+tip[:16]+'.json','FIXED_CENTRAL_READBACK_NAMESPACE')
            central,raw=read_json(args.central_readback,128*1024)
            local=independent_local_readback(config)
            require(local['boot_id']==admission['clock_boot_id'],'ADMITTED_CLOCK_BOOT_CHANGED')
            result=qualify_interval(manifest,manifest_sha,samples,central,local,datetime.now(timezone.utc))
            result.update(manifest_sha256=manifest_sha,journal_tip_sha256=tip,central_readback_sha256=hashlib.sha256(raw).hexdigest())
            print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps(dict(kind='MANAGED_OBSERVATION_QUALIFICATION_BLOCKED',code='FIXED_HISTORY_OR_READBACK_REFUSAL',
            measured_24h=False,physical_vpn_accepted=False,fleet_accepted=False,full_beta_accepted=False)))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
