#!/usr/bin/env python3
"""Finite Linux/root synthetic proof; default77 creates nothing. No real journals."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from operations import node_transaction_readonly as observer

SOURCE_PINS = {
    'operations/node_transaction_readonly.py': ('78f431403aa1a0146dc74356c0a172ae4'
                                             'e7f19886e7b2d10c5d8147b5a9ad8a9'),
    'operations/xui_owner_repair.py': ('f9b02a7b048d0464946b165d4aa7dea6'
                                       '17196d4991926b12462011fc3b691aea'),
}
PREFIX = 'wavemesh-node-status-native-'
CASES = ('empty', 'terminal', 'cli_terminal', 'unknown', 'missing', 'symlink', 'replaced_root', 'namespace')
EXPECTED_STOPS = {'unknown': 'STOP_TRANSACTION_STATUS_UNKNOWN', 'missing': 'STOP_OBSERVATION_IO',
                  'symlink': 'STOP_OBSERVATION_IO', 'replaced_root': 'STOP_ROOT_IDENTITY',
                  'namespace': 'STOP_NAMESPACE_CHANGED'}
MAX_SECONDS = 45
MAX_CHILD_SECONDS = 8
MAX_CHILD_BYTES = 1024
FAILURES = frozenset(('SOURCE_PIN','SOURCE_MODULE','FIXTURE_TREE_BOUNDS','FIXTURE_TREE_CHANGED',
    'FIXTURE_DEADLINE','FIXTURE_NAME','FIXTURE_MODE','FIXTURE_IDENTITY','FIXTURE_PATH',
    'FIXTURE_DATA_BOUNDS','FIXTURE_FILE','FIXTURE_WRITE','EXPECTED_STOP_MISMATCH',
    'OBSERVATION_RESULT','OBSERVER_CHANGED_FIXTURE','DESCRIPTOR_LEAK','CLI_CALL_BUDGET',
    'CLI_DEADLINE','CLI_OUTPUT_BOUNDS','CLI_OUTCOME','CLI_OUTCOME_UNKNOWN','CASE_BUDGET',
    'CLEANUP_UNBOUND','CLEANUP_PARENT','CLEANUP_RESIDUE','NATIVE_PRECONDITIONS'))


class Failure(RuntimeError):
    pass


def need(condition, code):
    if not condition:
        raise Failure(code)


def check_sources():
    for relative, expected in SOURCE_PINS.items():
        path = ROOT / relative
        need(path.is_file() and not path.is_symlink() and path.stat().st_size <= 128*1024,
             'SOURCE_PIN')
        need(hashlib.sha256(path.read_bytes()).hexdigest() == expected, 'SOURCE_PIN')
    need(Path(observer.__file__) == ROOT/'operations/node_transaction_readonly.py' and
         Path(observer.owner.__file__) == ROOT/'operations/xui_owner_repair.py', 'SOURCE_MODULE')


def directory_pin(info):
    observer.owner.safe_root_directory(info)
    return info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid


def snapshot(path):
    """Only this owned finite synthetic tree; no atime changes or symlink following."""
    result = {}
    def visit(current, relative):
        info = current.lstat()
        value = observer.owner.metadata(info)
        if stat.S_ISLNK(info.st_mode):
            result[relative] = (value, os.readlink(current))
        elif stat.S_ISDIR(info.st_mode):
            result[relative] = (value, None)
            fd = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOATIME)
            try:
                with os.scandir(fd) as entries:
                    names = []
                    for entry in entries:
                        need(len(names) < 8, 'FIXTURE_TREE_BOUNDS')
                        names.append(entry.name)
                for name in sorted(names):
                    visit(current/name, relative+'/'+name)
            finally:
                os.close(fd)
        else:
            need(stat.S_ISREG(info.st_mode) and info.st_size <= 4096, 'FIXTURE_TREE_BOUNDS')
            fd = os.open(current, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME)
            try:
                data = os.read(fd, 4097)
                need(len(data) == info.st_size and observer.owner.metadata(os.fstat(fd)) == value,
                     'FIXTURE_TREE_CHANGED')
                result[relative] = (value, data)
            finally:
                os.close(fd)
        need(len(result) <= 12, 'FIXTURE_TREE_BOUNDS')
    visit(path, '')
    return result


def fd_count():
    count=0
    with os.scandir('/proc/self/fd') as entries:
        for _ in entries:
            count += 1
            need(count <= 32,'FIXTURE_TREE_BOUNDS')
    return count


class Proof:
    def __init__(self):
        self.deadline = time.monotonic()+MAX_SECONDS
        self.parent = self.path = self.pin = self.parent_pin = None
        self.attempted = self.completed = self.cli_calls = 0
        self.case = 'preconditions'
        self.creation_started = False

    def tick(self):
        need(time.monotonic() <= self.deadline, 'FIXTURE_DEADLINE')

    def create(self):
        self.parent = observer.owner.open_absolute_directory('/root')
        self.parent_pin = directory_pin(os.fstat(self.parent))
        self.creation_started = True
        self.path = Path(tempfile.mkdtemp(prefix=PREFIX, dir='/root'))
        need(self.path.parent == Path('/root') and re.fullmatch(PREFIX+r'[a-z0-9_]{8}', self.path.name),
             'FIXTURE_NAME')
        self.pin = directory_pin(self.path.lstat())
        need(self.pin[2] == 0o700, 'FIXTURE_MODE')
        self.check_tree()
        (self.path/'transactions').mkdir(mode=0o700)

    def check_tree(self):
        self.tick()
        need(directory_pin(os.fstat(self.parent)) == self.parent_pin and
             directory_pin(os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)) == self.pin,
             'FIXTURE_IDENTITY')

    def binding(self):
        path = self.path/'transactions'
        chain = []
        current = Path('/')
        chain.append([current.stat().st_dev, current.stat().st_ino])
        for name in path.parts[1:]:
            current /= name
            info = current.lstat()
            directory_pin(info)
            chain.append([info.st_dev, info.st_ino])
        return {'schema':1, 'target':observer.owner.TARGETS[0], 'operation':'a'*32,
                'namespace':observer.owner.namespace_identity(),
                'roots':[{'path':str(path), 'chain':chain}]}

    def write(self, relative, value, *, replace=False):
        self.check_tree()
        path = self.path/relative
        need(path.is_relative_to(self.path) and '..' not in path.parts, 'FIXTURE_PATH')
        raw = (json.dumps(value, sort_keys=True)+'\n').encode()
        need(len(raw) <= 4096, 'FIXTURE_DATA_BOUNDS')
        flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_TRUNC if replace else os.O_CREAT | os.O_EXCL)
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            need(stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 0 and
                 stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, 'FIXTURE_FILE')
            pending = memoryview(raw)
            while pending:
                self.tick()
                count = os.write(fd,pending)
                need(count > 0,'FIXTURE_WRITE')
                pending=pending[count:]
        finally:
            os.close(fd)

    def exercise(self, case, binding, *, cli=False):
        self.case = case
        self.check_tree()
        before = snapshot(self.path)
        before_fds = fd_count()
        self.attempted += 1
        try:
            result = self.cli(binding) if cli else observer.observe(binding, binding['target'], binding['operation'])
        except observer.Stop as error:
            need(error.code == EXPECTED_STOPS.get(case), 'EXPECTED_STOP_MISMATCH')
        else:
            need(case not in EXPECTED_STOPS and result == {
                'status':'LOCAL_TRANSACTIONS_CLEAR_NOT_ADMISSION',
                'counts':{'rootsObserved':1,'transactionsObserved':0 if case=='empty' else 2,
                          'committed':0 if case=='empty' else 1,'rolledBack':0 if case=='empty' else 1},
                'rootSetCompleteness':'NOT_OBSERVED','operationalExclusion':'NOT_OBSERVED',
                'liveAdmission':False,'filesystemWrites':0}, 'OBSERVATION_RESULT')
        need(snapshot(self.path) == before, 'OBSERVER_CHANGED_FIXTURE')
        need(fd_count() == before_fds,'DESCRIPTOR_LEAK')
        self.check_tree()
        self.completed += 1

    def cli(self, binding):
        self.cli_calls += 1
        need(self.cli_calls == 1, 'CLI_CALL_BUDGET')
        source = ROOT/'operations/node_transaction_readonly.py'
        fd = os.open(self.path/'binding.json',os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME)
        proc = None
        try:
            with os.fdopen(fd,'rb') as stream:
                raw=stream.read(4097)
                need(len(raw) <= 4096,'FIXTURE_DATA_BOUNDS')
                stream.seek(0)
                proc = subprocess.Popen([sys.executable,'-B',str(source),'--observe-readonly',
                    '--target',binding['target'],'--operation',binding['operation'],
                    '--binding-sha256',hashlib.sha256(raw).hexdigest()], stdin=stream,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
            data = {proc.stdout:bytearray(),proc.stderr:bytearray()}
            deadline = min(self.deadline,time.monotonic()+MAX_CHILD_SECONDS)
            with selectors.DefaultSelector() as poll:
                for stream in data:
                    os.set_blocking(stream.fileno(),False)
                    poll.register(stream,selectors.EVENT_READ)
                while poll.get_map():
                    need(time.monotonic() < deadline,'CLI_DEADLINE')
                    for key,_ in poll.select(min(0.1,max(0,deadline-time.monotonic()))):
                        chunk=os.read(key.fileobj.fileno(),MAX_CHILD_BYTES+1)
                        if not chunk: poll.unregister(key.fileobj)
                        else:
                            data[key.fileobj].extend(chunk)
                            need(len(data[key.fileobj]) <= MAX_CHILD_BYTES,'CLI_OUTPUT_BOUNDS')
            need(proc.wait(timeout=max(0.001,deadline-time.monotonic())) == 0 and not data[proc.stderr],
                 'CLI_OUTCOME')
            return observer.parse_json(bytes(data[proc.stdout]),MAX_CHILD_BYTES)
        finally:
            if proc is not None:
                if proc.poll() is None: proc.kill()
                try: proc.wait(timeout=1)
                except subprocess.TimeoutExpired: raise Failure('CLI_OUTCOME_UNKNOWN') from None
                finally:
                    proc.stdout.close(); proc.stderr.close()

    def run(self):
        self.create()
        binding = self.binding()
        self.exercise('empty',binding)
        plan={'schema_version':1,'operation':'synthetic','pid':123,'started_at':'2026-10-19T01:02:03Z'}
        for name,status in (('synthetic-a','committed'),('synthetic-b','rolled_back')):
            (self.path/'transactions'/name).mkdir(mode=0o700)
            self.write('transactions/'+name+'/plan.json',plan)
            self.write('transactions/'+name+'/result.json',{'status':status})
        self.exercise('terminal',binding)
        self.write('binding.json',binding)
        self.exercise('cli_terminal',binding,cli=True)
        self.write('transactions/synthetic-a/result.json',{'status':'future_terminal'},replace=True)
        self.exercise('unknown',binding)
        self.write('transactions/synthetic-a/result.json',{'status':'committed'},replace=True)
        (self.path/'transactions/synthetic-b/plan.json').unlink()
        self.exercise('missing',binding)
        self.write('transactions/synthetic-b/plan.json',plan)
        bad=self.path/'transactions/synthetic-a/result.json'
        bad.unlink(); bad.symlink_to('../synthetic-b/result.json')
        self.exercise('symlink',binding)
        bad.unlink(); self.write('transactions/synthetic-a/result.json',{'status':'committed'})
        (self.path/'transactions').rename(self.path/'original-transactions')
        (self.path/'transactions').mkdir(mode=0o700)
        self.exercise('replaced_root',binding)
        (self.path/'transactions').rmdir()
        (self.path/'original-transactions').rename(self.path/'transactions')
        wrong=copy.deepcopy(binding); wrong['namespace']['mount'][1] += 1
        self.exercise('namespace',wrong)
        need(self.attempted == self.completed == len(CASES) and self.cli_calls == 1,'CASE_BUDGET')

    def cleanup(self):
        # Separate cleanup reserve; an expired proof budget must not skip cleanup.
        self.deadline = time.monotonic()+5
        if self.path is None:
            return not self.creation_started
        need(self.pin is not None and self.parent is not None,'CLEANUP_UNBOUND')
        self.check_tree()
        # rmtree's fd-based implementation is mandatory. No blind path removal.
        shutil.rmtree(self.path.name,dir_fd=self.parent)
        fresh=observer.owner.open_absolute_directory('/root')
        try:
            need(directory_pin(os.fstat(fresh)) == self.parent_pin,'CLEANUP_PARENT')
            try: os.stat(self.path.name,dir_fd=fresh,follow_symlinks=False)
            except FileNotFoundError: return True
            raise Failure('CLEANUP_RESIDUE')
        finally:
            os.close(fresh)


def main(argv=None):
    if (sys.argv[1:] if argv is None else argv) != ['--require-native']:
        print('{"status":"NATIVE_LOCAL_STATUS_INERT","createdTrees":0}')
        return 77
    proof=None; primary='PASS'; code='NONE'; cleanup=False; cleanup_code='NONE'
    try:
        need(sys.platform == 'linux' and os.geteuid() == os.getegid() == 0 and
             shutil.rmtree.avoids_symlink_attacks,'NATIVE_PRECONDITIONS')
        check_sources()
        proof=Proof(); proof.run(); check_sources()
    except Exception:
        primary='NATIVE_LOCAL_STATUS_STOP'
        error=sys.exc_info()[1]
        code=(str(error) if isinstance(error,Failure) and str(error) in FAILURES
              else error.code if isinstance(error,observer.Stop) and type(error.code) is str and error.code in observer.STOP_CODES
              else 'NATIVE_LOCAL_STATUS_UNKNOWN')
    finally:
        if proof is not None:
            try: cleanup=proof.cleanup()
            except Exception:
                cleanup=False
                error=sys.exc_info()[1]
                cleanup_code=str(error) if isinstance(error,Failure) and str(error) in FAILURES else 'CLEANUP_STOP'
            finally:
                if proof.parent is not None:
                    try: os.close(proof.parent)
                    except OSError: cleanup=False; cleanup_code='CLEANUP_DESCRIPTOR_STOP'
        else: cleanup=True
    print(json.dumps({'status':'NATIVE_LOCAL_STATUS_PASS' if primary=='PASS' and cleanup else 'NATIVE_LOCAL_STATUS_STOP',
                     'code':code,'case':proof.case if proof and proof.case in CASES else 'preconditions',
                     'attempted':proof.attempted if proof else 0,'completed':proof.completed if proof else 0,
                     'cliCalls':proof.cli_calls if proof else 0,'cleanup':cleanup,'cleanupCode':cleanup_code,
                     'zeroOwnedResidue':cleanup},separators=(',',':')))
    return 0 if primary=='PASS' and cleanup else 1


if __name__ == '__main__':
    raise SystemExit(main())
