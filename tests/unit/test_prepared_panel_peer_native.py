"""Real Linux peer/socket/FD primitive; not full approved Producer/core proof."""
from contextlib import contextmanager
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts/lib'))
import prepared_execution as execution

CHILD = '''import os,select,socket,sys,time
from pathlib import Path
root=Path(sys.argv[1]);held=[];replacement=None;tcp_peer=None;pending=False;due=None;deferred=sys.argv[2]=='defer'
unix=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);unix.bind(str(root/'snapshot.sock'));os.chmod(root/'snapshot.sock',0o600);unix.listen()
tcp=socket.socket(socket.AF_INET,socket.SOCK_STREAM);tcp.bind(('127.0.0.1',0));tcp.listen()
print(tcp.getsockname()[1],flush=True)
try:
 while True:
  readers=[unix,sys.stdin]+([] if pending else [tcp])
  ready,_,_=select.select(readers,[],[],0.01 if pending else 10)
  if tcp in ready:
   pending=True
   if not deferred:due=time.monotonic()
  if unix in ready:
   conn,_=unix.accept();held.append(conn)
  if sys.stdin in ready:
   command=sys.stdin.readline().strip()
   if command=='quit' or not command:break
   if command=='arm-delay':due=time.monotonic()+0.25;print('armed',flush=True)
   if command=='accept-now':due=time.monotonic()
   if command=='replace':
    fd=tcp_peer.fileno();tcp_peer.close();replacement=os.open('/dev/null',os.O_RDONLY)
    if replacement!=fd:os.dup2(replacement,fd);os.close(replacement);replacement=fd
    print('replaced',flush=True)
   if command=='probe':
    ready,_,_=select.select([tcp_peer],[],[],0.1)
    print(len(tcp_peer.recv(256,socket.MSG_PEEK)) if ready else 0,flush=True)
  if pending and due is not None and time.monotonic()>=due:
   tcp_peer,_=tcp.accept();held.append(tcp_peer);pending=False;due=None;print('accepted',flush=True)
finally:
 for item in held:item.close()
 if replacement is not None:os.close(replacement)
 unix.close();tcp.close()
'''


@unittest.skipUnless(sys.platform=='linux' and getattr(os,'geteuid',lambda:1)()==0,'actual Linux root SO_PEERCRED/proc socket binding required')
class PreparedPanelPeerNative(unittest.TestCase):
    def line(self,process):
        ready,_,_=select.select([process.stdout],[],[],5)
        self.assertTrue(ready,'own peer fixture did not report readiness')
        return process.stdout.readline(128).decode().strip()

    @contextmanager
    def peer(self,accept=True):
        with tempfile.TemporaryDirectory(prefix='wm-panel-peer-') as name:
            root=Path(name);script=root/'peer.py';script.write_text(CHILD,encoding='utf-8')
            process=subprocess.Popen([sys.executable,'-u',str(script),str(root),'normal' if accept else 'defer'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
            connection=None
            try:
                port=int(self.line(process));connection=socket.socket(socket.AF_INET,socket.SOCK_STREAM);connection.settimeout(3);connection.connect(('127.0.0.1',port))
                if accept:self.assertEqual(self.line(process),'accepted')
                with mock.patch.object(execution,'DIRECTORY',root),mock.patch.object(execution,'SNAPSHOT',root/'snapshot.sock'):
                    with execution._connection() as (_,pid,_):
                        self.assertEqual(pid,process.pid)
                        producer=execution._process(pid)
                        with open(f'/proc/{pid}/exe','rb') as executable,open(sys.executable,'rb') as expected:
                            self.assertEqual(execution._hash(executable),execution._hash(expected),'fixture executable pin changed')
                            identity=os.fstat(executable.fileno())
                            yield process,connection,producer
                            if process.poll() is None:
                                self.assertEqual(execution._process(pid),producer)
                                self.assertTrue(execution._same(identity,os.stat(f'/proc/{pid}/exe')))
            finally:
                if connection is not None:connection.close()
                if process.poll() is None:
                    try:process.stdin.write(b'quit\n');process.stdin.flush()
                    except BrokenPipeError:pass  # Exit status below still exposes an unexpected crash.
                timed_out=False
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    timed_out=True;process.terminate();process.wait(timeout=5)
                finally:
                    for stream in (process.stdin,process.stdout,process.stderr):stream.close()
                self.assertFalse(timed_out,'own peer fixture failed graceful cleanup')
                self.assertIn(process.returncode,(0,-signal.SIGTERM),'own peer fixture crashed')

    def test_actual_accepted_fd_binds_same_root_peercred_process_and_exec(self):
        with self.peer() as (process,connection,producer):
            first=execution._accepted_panel_peer(connection,producer,time.monotonic()+3)
            self.assertEqual(first,execution._accepted_panel_peer(connection,producer,time.monotonic()+3))
            self.assertEqual(os.readlink(first[1]),f'socket:[{first[0]}]')
            process.stdin.write(b'probe\n');process.stdin.flush();self.assertEqual(self.line(process),'0')
            connection.sendall(b'fixture-only')
            process.stdin.write(b'probe\n');process.stdin.flush();self.assertEqual(self.line(process),str(len(b'fixture-only')))

    def test_actual_different_pid_reused_start_fd_and_exited_peer_refuse(self):
        with self.peer() as (process,connection,producer):
            for changed in (execution._process(os.getpid()),{**producer,'start_ticks':'0'}):
                with self.assertRaises(execution.ExecutionUnavailable):execution._accepted_panel_peer(connection,changed,time.monotonic()+3)
            process.stdin.write(b'replace\n');process.stdin.flush();self.assertEqual(self.line(process),'replaced')
            with self.assertRaises((execution.ExecutionUnavailable,OSError)):execution._accepted_panel_peer(connection,producer,time.monotonic()+3)
            process.terminate();process.wait(timeout=5)
            with self.assertRaises((execution.ExecutionUnavailable,OSError)):execution._accepted_panel_peer(connection,producer,time.monotonic()+3)

    def test_actual_delayed_accept_waits_on_same_socket_without_sending_bytes(self):
        with self.peer(accept=False) as (process,connection,producer):
            process.stdin.write(b'arm-delay\n');process.stdin.flush();self.assertEqual(self.line(process),'armed')
            original=connection.fileno(),connection.getsockname(),connection.getpeername()
            with mock.patch.object(execution,'_producer_socket_fd',wraps=execution._producer_socket_fd) as descriptors:
                execution._accepted_panel_peer(connection,producer,time.monotonic()+3)
                self.assertGreaterEqual(len({call.args[1] for call in descriptors.call_args_list}),2,'queued LISTEN and later accepted FD were not both checked')
            self.assertEqual(self.line(process),'accepted')
            self.assertEqual((connection.fileno(),connection.getsockname(),connection.getpeername()),original)
            process.stdin.write(b'probe\n');process.stdin.flush();self.assertEqual(self.line(process),'0')

    def test_actual_accept_timeout_has_no_bytes_and_does_not_reconnect(self):
        with self.peer(accept=False) as (process,connection,producer):
            original=connection.fileno(),connection.getsockname(),connection.getpeername()
            started=time.monotonic()
            with self.assertRaisesRegex(execution.ExecutionUnavailable,'^PANEL_ACCEPT_TIMEOUT$'):
                execution._accepted_panel_peer(connection,producer,started+0.15)
            self.assertLess(time.monotonic()-started,2)
            self.assertEqual((connection.fileno(),connection.getsockname(),connection.getpeername()),original)
            process.stdin.write(b'accept-now\n');process.stdin.flush();self.assertEqual(self.line(process),'accepted')
            process.stdin.write(b'probe\n');process.stdin.flush();self.assertEqual(self.line(process),'0')


if __name__=='__main__':unittest.main()
