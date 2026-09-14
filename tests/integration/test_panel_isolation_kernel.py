#!/usr/bin/env python3
"""Real nftables packet tests, exclusively inside a disposable network/PID ns."""
import json
import os
import re
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
from panel_isolation import PanelIsolation, IsolationError, TABLE
from panel_request_guard import PanelRequestGuard, maintenance_node_lock, PanelRequestError

PORT = 31333
CONTROL = 31334
OP = "00000000-0000-4000-8000-000000000001"


def run(args, data=None):
    r = subprocess.run(args, input=data, capture_output=True, timeout=10)
    if r.returncode:
        raise RuntimeError("FIXTURE_COMMAND_FAILED")
    return r.stdout


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(20)
        try:
            while block := self.request.recv(32):
                self.request.sendall(block)
        except (TimeoutError, ConnectionError):
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Server6(Server):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


CLIENT = '''import os,socket,sys
if sys.argv[3]=="nobody":
    os.setgroups([]);os.setgid(65534);os.setuid(65534)
sock=socket.socket(socket.AF_INET6 if ":" in sys.argv[1] else socket.AF_INET)
sock.settimeout(0.4)
try:
    sock.connect((sys.argv[1],int(sys.argv[2])))
    print("CONNECTED",flush=True)
except OSError:
    print("BLOCKED",flush=True);sys.exit(0)
for line in sys.stdin:
    try:
        sock.sendall(b"probe")
        print("PASS" if sock.recv(5)==b"probe" else "BLOCKED",flush=True)
    except OSError:
        print("BLOCKED",flush=True)
'''


def main():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("PRIVATE_LINUX_ROOT_REQUIRED")
    parent = os.environ.get("WAVEMESH_CI_PARENT_NETNS")
    assert parent and os.readlink('/proc/self/ns/net') != parent
    assert [row['ifname'] for row in json.loads(run(['ip','-j','link']))] == ['lo']
    run(['ip','link','set','lo','up'])
    children, servers = [], []
    def start(args):
        child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        children.append(child)
        return child
    def line(child):
        timer = threading.Timer(5, child.kill)
        timer.start()
        try:
            return child.stdout.readline().strip()
        finally:
            timer.cancel()
    def connect(address, port=PORT, role="nobody", remote=None, expected=True):
        prefix = ['nsenter','-t',str(remote.pid),'-n'] if remote else []
        child = start([*prefix, sys.executable, '-c', CLIENT, address, str(port), role])
        assert line(child) == ('CONNECTED' if expected else 'BLOCKED'), 'CONNECT_RESULT_MISMATCH'
        return child
    def probe(child, expected=True):
        child.stdin.write('probe\n');child.stdin.flush()
        assert line(child) == ('PASS' if expected else 'BLOCKED'), 'PACKET_RESULT_MISMATCH'
    try:
        for kind, address in ((Server,'0.0.0.0'),(Server6,'::')):
            for port in (PORT,CONTROL):
                server = kind((address,port),Echo)
                servers.append(server)
                threading.Thread(target=server.serve_forever,daemon=True).start()
        # Deliberately permit established traffic in an EARLIER base chain.
        # Accept in another chain must not bypass our later terminal drop.
        run(['/usr/sbin/nft','-f','-'], b'''add table inet fixture_existing
add chain inet fixture_existing input { type filter hook input priority -400; policy accept; }
add chain inet fixture_existing output { type filter hook output priority -400; policy accept; }
add rule inet fixture_existing input ct state established,related accept
add rule inet fixture_existing output ct state established,related accept
''')
        other_before = run(['/usr/sbin/nft','-j','list','table','inet','fixture_existing'])
        remote = start(['unshare','--net',sys.executable,'-c','import sys;print("READY",flush=True);sys.stdin.read()'])
        assert line(remote) == 'READY'
        run(['ip','link','add','wm-host','type','veth','peer','name','wm-peer'])
        run(['ip','link','set','wm-peer','netns',str(remote.pid)])
        run(['ip','addr','add','198.18.0.1/30','dev','wm-host'])
        run(['ip','-6','addr','add','fd00:1234::1/64','dev','wm-host','nodad'])
        run(['ip','link','set','wm-host','up'])
        for args in (['link','set','lo','up'],['addr','add','198.18.0.2/30','dev','wm-peer'],
                     ['-6','addr','add','fd00:1234::2/64','dev','wm-peer','nodad'],['link','set','wm-peer','up']):
            run(['nsenter','-t',str(remote.pid),'-n','ip',*args])
        blocked = [connect('127.0.0.1'),connect('::1'),
                   connect('198.18.0.1',role='root',remote=remote),
                   connect('fd00:1234::1',role='root',remote=remote)]
        controls = [connect('127.0.0.1',role='root'),connect('::1',role='root'),
                    connect('127.0.0.1',CONTROL),connect('::1',CONTROL),
                    connect('198.18.0.1',CONTROL,'root',remote),connect('fd00:1234::1',CONTROL,'root',remote)]
        for child in blocked + controls:
            probe(child)
        print('PREEXISTING_IPV4_IPV6_CONNECTIONS=PASS',flush=True)
        with tempfile.TemporaryDirectory(prefix='wm-isolation-') as directory:
            root = Path(directory)
            guard = PanelRequestGuard(root/'journal')
            lock = root/'node.lock'
            with maintenance_node_lock(lock), guard.locked():
                guard.maintenance('prepare',OP,1)
            isolation = PanelIsolation()
            original_apply = isolation.apply
            def lost_result(expected):
                # The real kernel transaction commits, but the caller loses its
                # result. The next invocation must observe, never apply twice.
                assert json.loads((guard.root/'state.json').read_text())['schema_version'] == 3
                original_apply(expected)
                raise IsolationError('SYNTHETIC_LOST_RESULT')
            with patch.object(isolation,'apply',side_effect=lost_result):
                try:
                    isolation.isolate(guard,OP,1,'a'*64,'b'*64,PORT,lock)
                except IsolationError:
                    pass
                else:
                    raise AssertionError('LOST_RESULT_NOT_REPORTED')
            before = (guard.root/'state.json').read_bytes()
            with patch.object(isolation,'apply',side_effect=AssertionError('SECOND_APPLY')):
                try:
                    receipt = isolation.isolate(guard,OP,1,'a'*64,'b'*64,PORT,lock)
                except IsolationError:
                    # Only our synthetic CI table, stripped of handles/comment
                    # values. Never dump the host ruleset or external metadata.
                    for item in (isolation.observe() or {}).get('nftables',[]):
                        for kind, fields in item.items():
                            if kind in {'table','chain','rule'}:
                                safe = {k:v for k,v in fields.items() if k not in {'handle','comment'}}
                                print('CI_POLICY_SHAPE='+json.dumps({kind:safe}),flush=True)
                    raise
            assert receipt['reconciliation_required'] and receipt['quiescence']=='NOT_PROVEN'
            assert (guard.root/'state.json').read_bytes()==before
            print('COMMITTED_LOST_RESULT_RECONCILED_WITHOUT_REAPPLY=PASS',flush=True)
            for child in blocked:
                probe(child,False)
            for child in controls:
                probe(child)
            connect('127.0.0.1',expected=False)
            connect('::1',expected=False)
            connect('198.18.0.1',role='root',remote=remote,expected=False)
            connect('fd00:1234::1',role='root',remote=remote,expected=False)
            print('NEW_AND_ESTABLISHED_API_CONNECTIONS_BLOCKED=PASS',flush=True)
            print('ROOT_LOOPBACK_AND_OTHER_PORT_CONTROLS_HEALTHY=PASS',flush=True)
            assert run(['/usr/sbin/nft','-j','list','table','inet','fixture_existing'])==other_before
            with maintenance_node_lock(lock), guard.locked():
                try:
                    guard.maintenance('cancel',OP,1)
                except PanelRequestError:
                    pass
                else:
                    raise AssertionError('INSTALLATION_CANCELLED')
            # Tamper only inside this disposable namespace. The implementation
            # must reject drift, never overwrite it or repair by flushing.
            run(['/usr/sbin/nft','add','rule','inet',TABLE,'input','accept'])
            drifted = isolation.observe()
            try:
                isolation.isolate(guard,OP,1,'a'*64,'b'*64,PORT,lock)
            except IsolationError:
                pass
            else:
                raise AssertionError('DRIFT_ACCEPTED')
            assert isolation.observe()==drifted
            run(['/usr/sbin/nft','delete','table','inet',TABLE])
            try:
                isolation.isolate(guard,OP,1,'a'*64,'b'*64,PORT,lock)
            except IsolationError as exc:
                assert str(exc)=='ISOLATION_ABSENT_RECONCILIATION_REQUIRED'
            else:
                raise AssertionError('ABSENT_RULES_REAPPLIED')
            assert isolation.observe() is None
            assert (guard.root/'state.json').read_bytes()==before
            print('DRIFT_AND_REMOVED_RULES_REJECT_WITHOUT_MUTATION=PASS',flush=True)
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        for server in servers:
            server.shutdown();server.server_close()


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        # No raw kernel config, tokens, network endpoints or subprocess output.
        print('PANEL_ISOLATION_KERNEL=FAILED; TYPE='+type(exc).__name__,file=sys.stderr)
        if isinstance(exc,(IsolationError,AssertionError,RuntimeError)) and re.fullmatch('[A-Z_]+',str(exc)):
            print('CODE='+str(exc),file=sys.stderr)
        raise SystemExit(1)
    print('PANEL_ISOLATION_KERNEL=PASS; SCOPE=DISPOSABLE_CI_NAMESPACE')
