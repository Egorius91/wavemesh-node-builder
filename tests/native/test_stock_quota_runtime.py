"""Root-only owned offline VM: stock panel + full Agent/CLI quota denial.

Requires pinned public3x-ui3.11.0 archive, systemd and a private loopback-only
network namespace. Uses ephemeral CA/identities/config and an exclusive fixture
x-ui.service. No live Node/nginx/public subscription/service acceptance claim.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import sqlite3
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

ARCHIVE_SHA = "0ddfa4702f595442e2962acd81addc768fdacaa4ff2672c971a0463dae744916"
XUI_SHA = "fb7b3776334fce69299f90168c8c31377b69f89c7787088e6dbf561d07b3d345"
XRAY_SHA = "a27e3f95ab6ce270956d0cecd32eda4686a811afc71c64a6a734a3dede14883e"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def eventually(probe, seconds=30):
    end = time.monotonic() + seconds
    last = None
    while time.monotonic() < end:
        try:
            value = probe()
            if value:
                return value
        except (OSError, ValueError, AssertionError, RuntimeError) as exc:
            last = type(exc).__name__
        time.sleep(.1)
    raise AssertionError(f"Bounded stock precondition was not met ({last})")


def receive(sock, count):
    result = b""
    while len(result) < count:
        part = sock.recv(count - len(result))
        if not part:
            raise ConnectionError("Owned synthetic stream ended")
        result += part
    return result


def exchange(sock):
    payload = b"wm-stock-fixture:" + os.urandom(512)
    sock.sendall(payload)
    assert receive(sock, len(payload)) == payload
    return len(payload)


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(1)
        while not self.server.stopping.is_set():
            try:
                data = self.request.recv(65536)
                if not data:
                    return
                self.request.sendall(data)
            except socket.timeout:
                continue
            except OSError:
                return


class EchoServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


def socks_connection(port, destination, *, allow_upstream_refusal=False):
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    try:
        sock.sendall(b"\x05\x01\x00")
        assert receive(sock, 2) == b"\x05\x00"
        try:
            sock.sendall(b"\x05\x01\x00\x01\x7f\x00\x00\x01" + struct.pack("!H", destination))
            reply = receive(sock, 4)
            assert reply[:2] == b"\x05\x00" and reply[3] in (1, 3, 4)
            length = {1: 4, 4: 16}.get(reply[3])
            if length is None:
                length = receive(sock, 1)[0]
            receive(sock, length + 2)
            exchange(sock)  # A SOCKS success alone does not prove a tunnel.
        except (OSError, AssertionError):
            if not allow_upstream_refusal:
                raise
            sock.close()
            return None
        return sock
    except BaseException:
        sock.close()
        raise


def engine_children(panel_pid, engine):
    """Read only direct engine children of this exact owned panel process."""
    found = []
    for directory in Path("/proc").iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            status = (directory / "status").read_text()
            parent = int(re.search(r"^PPid:\s*(\d+)$", status, re.M)[1])
            if parent == panel_pid and (directory / "exe").resolve() == engine.resolve():
                # PID + kernel starttime prevents PID-reuse confusion.
                tail = (directory / "stat").read_text().rsplit(") ", 1)[1].split()
                found.append((int(directory.name), tail[19]))
        except (OSError, ValueError, TypeError):
            continue
    return found


def systemctl(*arguments):
    result = subprocess.run(["/usr/bin/systemctl", *arguments], capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, "Owned fixture service operation failed"
    return result.stdout


def unit():
    return dict(line.split("=", 1) for line in systemctl("show", "x-ui.service", "--property=LoadState,ActiveState,MainPID,InvocationID").splitlines())


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module; spec.loader.exec_module(module)
    return module


def run_case(runtime, agent, archive, node_source):
    processes, streams, handles, ports, known_engine_pids = [], [], [], [], set()
    case = {"scope": "actual Agent validator/capability/CLI/fence + stock3x-ui/Xray + owned systemd process group"}
    unit_path = Path("/run/systemd/system/x-ui.service")
    config_path = Path("/etc/wavemesh-node/config.json")
    assert unit()["LoadState"] == "not-found" and not unit_path.exists() and not unit_path.is_symlink()
    assert not config_path.parent.exists() and not Path("/etc/x-ui").exists()
    installed = False; unit_bytes = None; config_bytes = None
    with tempfile.TemporaryDirectory(prefix="wm-pr2-executor-") as name:
        root = Path(name)
        assert root.parent == Path("/tmp") and root.name.startswith("wm-pr2-executor-")
        with tarfile.open(archive) as tar:
            entries = tar.getmembers()
            assert all((m.isfile() or m.isdir()) and not Path(m.name).is_absolute() and ".." not in Path(m.name).parts for m in entries)
            tar.extractall(root, filter="data")
        panel_exe, engine = root / "x-ui/x-ui", root / "x-ui/bin/xray-linux-amd64"
        assert digest(panel_exe) == XUI_SHA and digest(engine) == XRAY_SHA
        db_dir = root / "db"
        db_dir.mkdir(mode=0o700)
        (root / "logs").mkdir(mode=0o700)
        panel_port, api_port, inbound_port, destination = (available_port() for _ in range(4))
        ports.extend((panel_port, api_port, inbound_port, destination))
        assert len(set(ports)) == len(ports)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C.UTF-8", "TZ": "UTC",
               "XUI_DB_FOLDER": str(db_dir), "XUI_BIN_FOLDER": str(engine.parent),
               "XUI_LOG_FOLDER": str(root / "logs"), "XUI_DB_TYPE": "sqlite", "XUI_DB_DSN": "",
               "XUI_LOG_LEVEL": "error"}
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=stock.example.invalid",
                        "-addext", "subjectAltName=DNS:stock.example.invalid", "-days", "1"],
                       env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, timeout=30)
        key.chmod(0o600)
        for label, executable, flag, expected in (("panel", panel_exe, "-v", "3.11.0"),
                                                   ("engine", engine, "version", "26.10.21")):
            version = subprocess.run([str(executable), flag], env=env, cwd=panel_exe.parent,
                                     capture_output=True, text=True, timeout=30)
            assert version.returncode == 0 and expected in version.stdout, "Pinned stock binary version mismatch"
            case[label + "_version"] = version.stdout.strip().splitlines()[0]
        cli = subprocess.run([str(panel_exe), "setting", "-port", str(panel_port), "-listenIP", "127.0.0.1",
                              "-webBasePath", "/pr2-stock-only/", "-username", "synthetic-fixture",
                              "-password", "disposable-local-fixture", "-getApiToken", "-tokenName", "pr2-stock-only"],
                             env=env, cwd=panel_exe.parent, capture_output=True, text=True, timeout=90)
        assert cli.returncode == 0, "Synthetic stock settings command failed"
        tokens = re.findall(r"^apiToken:\s*([A-Za-z0-9_-]+)\s*$", cli.stdout, re.M)
        assert len(tokens) == 1, "Synthetic stock token was not created"
        db_path = db_dir / "x-ui.db"
        assert db_path.is_file()
        # Derive the loopback-only API/statistics template from the pinned
        # release's native shape, allowing only our fixture's local echo target.
        template = {"api": {"services": ["HandlerService", "LoggerService", "StatsService", "RoutingService"], "tag": "api"},
                    "inbounds": [{"listen": "127.0.0.1", "port": api_port, "protocol": "tunnel",
                                  "settings": {"rewriteAddress": "127.0.0.1"}, "tag": "api"}],
                    "log": {"access": "none", "loglevel": "debug", "error": str(root / "engine.log")}, "stats": {},
                    "outbounds": [{"protocol": "freedom", "tag": "direct", "settings": {"finalRules": [
                        {"action": "allow", "network": "tcp", "ip": ["127.0.0.1/32"], "port": str(destination)}]}},
                        {"protocol": "blackhole", "tag": "blocked"}],
                    "policy": {"levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
                               "system": {"statsInboundUplink": True, "statsInboundDownlink": True}},
                    "routing": {"domainStrategy": "AsIs", "rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]}}
        with sqlite3.connect(db_path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(settings)")}
            assert {"key", "value"}.issubset(columns)
            for setting_key, value in {"restartXrayOnClientDisable": str(False).lower(),
                               "subEnable": "false", "xrayTemplateConfig": json.dumps(template)}.items():
                changed = connection.execute("UPDATE settings SET value=? WHERE key=?", (value, setting_key))
                if not changed.rowcount:
                    connection.execute("INSERT INTO settings(key,value) VALUES(?,?)", (setting_key, value))
            assert connection.execute("SELECT value FROM settings WHERE key='restartXrayOnClientDisable'").fetchone() == (str(False).lower(),)
        panel = runtime.PanelClient({"panel": {"listen_port": panel_port, "path": "/pr2-stock-only/", "api_auth": {"token": tokens[0]}}}, timeout=3)

        def start(arguments, label):
            handle = (root / f"{label}.log").open("wb")
            handles.append(handle)
            process = subprocess.Popen(arguments, env=env, cwd=panel_exe.parent, stdout=handle, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            processes.append(process)
            return process

        server = EchoServer(("127.0.0.1", destination), Echo)
        server.stopping = threading.Event()
        echo_thread = threading.Thread(target=server.serve_forever, daemon=True)
        echo_thread.start()
        try:
            assert len(ports) == len(set(ports))
            # systemd launches outside our caller's network namespace unless
            # explicitly bound to it. Both fixture service and clients use only
            # this process's loopback namespace; no host/guest external route.
            lines = ["[Unit]", "Description=Disposable stock quota executor fixture", "[Service]",
                "Type=simple", "KillMode=control-group", "SendSIGKILL=yes", "Restart=no", "TimeoutStopSec=5s",
                f"NetworkNamespacePath=/proc/{os.getpid()}/ns/net", f"WorkingDirectory={panel_exe.parent}",
                *[f"Environment={k}={v}" for k, v in env.items()], f"ExecStart={panel_exe} run"]
            unit_bytes = ("\n".join(lines) + "\n").encode()
            with os.fdopen(os.open(unit_path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), "wb") as stream:
                stream.write(unit_bytes)
            installed = True
            systemctl("daemon-reload"); systemctl("start", "x-ui.service")
            eventually(lambda: panel.call("GET", "/panel/api/inbounds/list").get("success"))
            result = panel.call("POST", "/panel/api/inbounds/add", {
                "up": 0, "down": 0, "total": 0, "remark": "Synthetic PR2 executor fixture", "enable": True,
                "expiryTime": 0, "listen": "127.0.0.1", "port": inbound_port, "protocol": "vless",
                "settings": json.dumps({"clients": [], "decryption": "none", "fallbacks": []}),
                "streamSettings": json.dumps({"network": "xhttp", "security": "tls",
                    "tlsSettings": {"certificates": [{"certificateFile": str(cert), "keyFile": str(key)}]},
                    "xhttpSettings": {"path": "/stock-fixture/", "host": "stock.example.invalid", "mode": "stream-one"}}),
                "sniffing": json.dumps({"enabled": False}), "allocate": json.dumps({"strategy": "always", "refresh": 5, "concurrency": 3})})
            assert result.get("success") is True and type(result.get("obj", {}).get("id")) is int
            inbound_id = result["obj"]["id"]
            config = {"panel": {"listen_port": panel_port, "path": "/pr2-stock-only/", "api_auth": {"token": tokens[0]}},
                "server": {"domain": "stock.example.invalid"}, "network": {"subscription": {"backend": "xui-native", "path": "/opaque/subscription/"}}}
            config_path.parent.mkdir(mode=0o700)
            config_bytes = (json.dumps(config) + "\n").encode()
            with os.fdopen(os.open(config_path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), "wb") as stream:
                stream.write(config_bytes)
            instance = agent.NodeAgent.__new__(agent.NodeAgent)
            instance.config = SimpleNamespace(access_runtime_path=node_source/"agent/access_runtime.py",
                env_path=root/"agent.env", access_state_root=root/"access")
            assert instance.placement_runtime_ready(3), "Actual CLI capability does not qualify the new protocol"
            epoch = str(uuid.uuid4())
            budget = dict(epoch_id=epoch, baseline_receipt_id="synthetic-baseline", credit_receipt_id="synthetic-credit",
                credit_revision=1, counter_baseline_bytes="0", counter_identity_hash=None, counter_source_receipt_id=None, credit_bytes="1048576")
            def request(version, enabled, traffic_budget, operation="access.update_entitlements"):
                payload = dict(access_id="access_stock_001", placement_id="legacy:access_stock_001", generation=1,
                    entitlement_revision=4, plan_hash="a"*64, command_id=f"command{version:03}", desired_version=version,
                    enabled=enabled, expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(), device_limit=0,
                    quota_bytes="1048576", quota_protocol_version=2, traffic_budget=traffic_budget)
                envelope = dict(schema_version=3, tenant_id="tenant001", target_node_id="node0001", command_id=payload["command_id"],
                    expected_version=version-1, type=operation, attempt=1, payload=payload)
                return agent.validate_access_command(envelope, "node0001", "tenant001")[3]
            initial = unit()
            absent = instance.execute_access_runtime("access.update_entitlements", request(1, False, None))
            assert absent["disabled_identity_count"] == 0 and absent["traffic_counters"] == []
            assert unit()["InvocationID"] == initial["InvocationID"], "New empty target restarted the panel"
            provision_request = request(2, True, budget, "access.provision")
            profile = instance.execute_access_runtime("access.provision", provision_request)
            assert profile["quota_protocol_version"] == 2
            states = [profile, {"panel_email": "wm_stock_sentinel", "client_uuid": str(uuid.uuid4()), "sub_id": "stock_synthetic_sentinel"}]
            sentinel = states[1]
            added = panel.call("POST", "/panel/api/clients/add", {"client": {
                "email": sentinel["panel_email"], "id": sentinel["client_uuid"], "subId": sentinel["sub_id"],
                "enable": True, "limitIp": 0, "totalGB": 1048576, "expiryTime": 0,
                "tgId": 0, "flow": "", "reset": 0, "resetDay": 0, "resetWeekday": 0}, "inboundIds": [inbound_id]})
            assert added.get("success") is True
            client_ports = []
            for index, state in enumerate(states):
                port = available_port(); assert port not in ports; ports.append(port); client_ports.append(port)
                client = {"log": {"loglevel": "error"}, "inbounds": [{"listen": "127.0.0.1", "port": port,
                    "protocol": "socks", "settings": {"auth": "noauth", "udp": False}}],
                    "outbounds": [{"protocol": "vless", "settings": {"vnext": [{"address": "127.0.0.1", "port": inbound_port,
                        "users": [{"id": state["client_uuid"], "encryption": "none"}]}]},
                        "streamSettings": {"network": "xhttp", "security": "tls",
                            "tlsSettings": {"serverName": "stock.example.invalid", "disableSystemRoot": True,
                                "certificates": [{"certificateFile": str(cert), "usage": "verify"}]},
                            "xhttpSettings": {"path": "/stock-fixture/", "host": "stock.example.invalid", "mode": "stream-one"}}}]}
                path = root/f"client-{index}.json"; path.write_text(json.dumps(client))
                start([str(engine), "run", "-config", str(path)], f"client-{index}")
            for port in client_ports:
                streams.append(eventually(lambda port=port: socks_connection(port, destination)))
            before_unit = unit(); old = eventually(lambda: engine_children(int(before_unit["MainPID"]), engine)); assert len(old) == 1
            known_engine_pids.update(old)
            for _ in range(8):
                for stream in streams: exchange(stream)
            before = eventually(lambda: positive_counter(runtime, panel, profile))
            observed_request = request(3, True, budget)
            observed = instance.execute_access_runtime("access.update_entitlements", observed_request)
            runtime.assert_quota_counter_progress(before, observed["traffic_counters"][0])
            assert instance.execute_access_runtime("access.update_entitlements", observed_request) == observed
            assert unit()["InvocationID"] == before_unit["InvocationID"]
            denial_request = request(4, False, budget)
            started = time.monotonic()
            denied = instance.execute_access_runtime("access.update_entitlements", denial_request)
            fresh = unit(); assert fresh["InvocationID"] != before_unit["InvocationID"]
            assert not Path("/proc/"+before_unit["MainPID"]).exists()
            current = eventually(lambda: engine_children(int(fresh["MainPID"]), engine)); assert len(current) == 1
            known_engine_pids.update(current)
            assert all(not Path(f"/proc/{pid}").exists() for pid, _ in old)
            assert not can_exchange(streams[0]), "Old target stream survived accepted denial"
            assert not can_exchange(streams[1]), "Full panel restart impact on sentinel was not observed"
            assert denied["quota_protocol_version"] == 2 and denied["disabled_identity_count"] == 1
            assert denied["quota_history_complete"] is True
            runtime.assert_quota_counter_progress(observed["traffic_counters"][0], denied["traffic_counters"][0])
            assert refused(client_ports[0], destination)
            streams.append(eventually(lambda: socks_connection(client_ports[1], destination)))
            assert refused(client_ports[0], destination), "Target reopened after healthy sentinel"
            fence_path = root/"access/access_stock_001.fence"
            fence_bytes = fence_path.read_bytes(); fence = json.loads(fence_bytes)
            assert fence["execution_complete"] and fence["quota_epochs"][epoch]["terminal"]
            assert fence["quota_material"] == denied
            assert instance.execute_access_runtime("access.update_entitlements", denial_request) == denied
            assert fence_path.read_bytes() == fence_bytes and unit()["InvocationID"] == fresh["InvocationID"]
            client = runtime.get_client(panel, profile["panel_email"])["client"]
            assert client["enable"] is False and client["totalGB"] == 1048576
            case.update(status="PASS", new_empty_target_no_restart=True, actual_validator_cli_material=True,
                old_target_flow_ended=True, old_sentinel_flow_ended=True, new_target_auth_refused=True,
                new_sentinel_works=True, cached_enabled_and_deny_no_restart_or_refill=True, counter_highwater_preserved=True,
                before_counter=observed["traffic_counters"][0], final_counter=denied["traffic_counters"][0],
                deny_elapsed_seconds=round(time.monotonic()-started,3), old_unit=before_unit, fresh_unit=fresh)
        except Exception as failure:
            # Retain bounded synthetic logs, omit tokens and certificate/key data.
            diagnostic = {"failure": type(failure).__name__, "message": str(failure)[:300], "unit": unit(), "logs": {}}
            for path in root.glob("*.log"):
                diagnostic["logs"][path.name] = [line[:500] for line in path.read_text(errors="replace").splitlines()[-12:]
                    if not re.search(r"apiToken|Bearer|privateKey|password", line, re.I)]
            print(json.dumps({"diagnostic": diagnostic}, sort_keys=True), flush=True)
            raise
        finally:
            for stream in streams: stream.close()
            if installed:
                assert unit_path.read_bytes() == unit_bytes
                systemctl("stop", "x-ui.service")
                assert unit()["ActiveState"] == "inactive"
                unit_path.unlink(); systemctl("daemon-reload")
                assert unit()["LoadState"] == "not-found"
            if config_bytes is not None:
                assert config_path.read_bytes() == config_bytes
                config_path.unlink(); config_path.parent.rmdir()
            server.stopping.set(); server.shutdown(); server.server_close(); echo_thread.join(timeout=2)
            for process in reversed(processes):
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
            for process in reversed(processes):
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
            for handle in handles: handle.close()
            assert all(process.poll() is not None for process in processes)
            for pid, starttime in known_engine_pids:
                stat = Path(f"/proc/{pid}/stat")
                assert not stat.exists() or stat.read_text().rsplit(") ",1)[1].split()[19] != starttime
            for port in ports:
                with socket.socket() as check: assert check.connect_ex(("127.0.0.1",port)) != 0
            print("STOCK_EXECUTOR_OWNED_SERVICE_CONFIG_PROCESSES_CLEANUP_PASS",flush=True)
        case["owned_unit_config_processes_listeners_absent"] = True
    assert not root.exists()
    case["owned_fixture_absent"] = True
    return case


def positive_counter(runtime, panel, state):
    value = runtime.read_quota_counter(panel, state)
    return value if int(value["up_bytes"]) + int(value["down_bytes"]) > 0 else None


def can_exchange(stream):
    try:
        exchange(stream)
        return True
    except (OSError, AssertionError):
        return False


def refused(port, destination):
    # Frontend TCP and SOCKS negotiation must succeed; only failure farther
    # upstream qualifies as denial. Recheck after the new sentinel works.
    stream = socks_connection(port, destination, allow_upstream_refusal=True)
    if stream is None:
        return True
    stream.close()
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args()
    assert sys.platform == "linux" and os.geteuid() == 0
    assert socket.if_nameindex() == [(1,"lo")], "Use an owned private loopback-only network namespace"
    assert digest(args.archive) == ARCHIVE_SHA and not args.output.exists()
    node_source = Path(__file__).resolve().parents[2]
    runtime = load_module("stock_executor_runtime",node_source/"agent/access_runtime.py")
    agent = load_module("stock_executor_agent",node_source/"agent/node_agent.py")
    result = {"status":"RUNNING","archive_sha256":ARCHIVE_SHA,
        "runtime_sha256":digest(node_source/"agent/access_runtime.py"),"agent_sha256":digest(node_source/"agent/node_agent.py")}
    args.output.write_text(json.dumps(result)+"\n")
    try:
        result["case"] = run_case(runtime,agent,args.archive,node_source); result["status"] = "PASS"
    except BaseException:
        result["status"] = "FAIL"; raise
    finally:
        args.output.write_text(json.dumps(result,sort_keys=True,indent=2)+"\n")
        print(json.dumps(result,sort_keys=True),flush=True)


if __name__ == "__main__": main()
