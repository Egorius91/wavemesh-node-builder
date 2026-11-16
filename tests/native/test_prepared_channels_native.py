#!/usr/bin/env python3
"""Exact Xray router/underlay proof in an owned, loopback-only network namespace.

Not 3X-UI/nginx/systemd, RF, provider geography, active-session preservation or
SaaS admission proof. Parent namespace/network and installed services are untouched.
"""
import argparse
import copy
import hashlib
import http.client
import http.server
import importlib.util
import ipaddress
import json
import os
import re
import signal
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/lib"))
import prepared_channels as channels

ENGINE_SHA256 = "a27e3f95ab6ce270956d0cecd32eda4686a811afc71c64a6a734a3dede14883e"
PROXY_DIAGNOSTICS = {}


def receive(sock, count):
    data = b""
    while len(data) < count:
        part = sock.recv(count-len(data))
        if not part:
            raise ConnectionError("short SOCKS reply")
        data += part
    return data


def socks_command(port, command, address, destination):
    stream = socket.create_connection(("127.0.0.1", port), timeout=2)
    stream.sendall(b"\x05\x01\x00")
    if receive(stream, 2) != b"\x05\x00":
        stream.close(); raise ConnectionError("SOCKS authentication failed")
    ip = ipaddress.ip_address(address)
    stream.sendall(bytes((5, command, 0, 1 if ip.version == 4 else 4)) + ip.packed + destination.to_bytes(2, "big"))
    reply = receive(stream, 4)
    if reply[:2] != b"\x05\x00":
        stream.close(); raise ConnectionError("SOCKS command refused")
    bound = socket.inet_ntop(socket.AF_INET if reply[3] == 1 else socket.AF_INET6, receive(stream, 4 if reply[3] == 1 else 16))
    bound_port = int.from_bytes(receive(stream, 2), "big")
    return stream, bound, bound_port


def proxy_get(port, address, destination):
    result = b""
    try:
        stream, _, _ = socks_command(port, 1, address, destination)
        with stream:
            stream.sendall(b"GET / HTTP/1.0\r\nHost: fixture.example\r\n\r\n")
            response = http.client.HTTPResponse(stream); response.begin(); result = response.read()
        PROXY_DIAGNOSTICS[port] = {"bytes": len(result), "status200": response.status == 200, "pathMarkers": [p for p in ("upcloud-v4", "upcloud-v6", "infomaniak-v4", "infomaniak-v6") if p.encode() in result]}
        return result
    except (OSError, ConnectionError) as error:
        PROXY_DIAGNOSTICS[port] = {"error": type(error).__name__, "partialBytes": len(result), "pathMarkers": [p for p in ("upcloud-v4", "upcloud-v6", "infomaniak-v4", "infomaniak-v6") if p.encode() in result]}
        return b""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); return sock.getsockname()[1]


def proxy_dns(port, destination):
    query = bytes.fromhex("123401000001000000000000") + b"\x07example\x03com\x00\x00\x01\x00\x01"
    try:
        control, host, relay = socks_command(port, 3, "0.0.0.0", 0)
        with control, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
            datagram.settimeout(1)
            datagram.sendto(b"\x00\x00\x00\x01\x7f\x00\x00\x01"+destination.to_bytes(2, "big")+query, (host if host != "0.0.0.0" else "127.0.0.1", relay))
            result, _ = datagram.recvfrom(4096)
            return result.endswith(query[:2]+b"\x81\x80"+query[4:])
    except (OSError, ConnectionError):
        return False


def run(binary, diagnose=False):
    spec = importlib.util.spec_from_file_location("prepared_fixture", ROOT/"tests/unit/test_prepared_channels.py")
    fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
    config, plan, template = fixture.fixture()
    engine = {"name": "xray", "version": "26.10.21", "sha256": ENGINE_SHA256}
    plan["channel"] = channels.channel_descriptor(engine)
    for approval in plan["exits"]: approval["channel"] = copy.deepcopy(plan["channel"])
    plan["health"].update(sample_interval_seconds=1, recovery_samples=2, failure_samples=2, cooldown_seconds=0, max_age_seconds=30)
    channels.check_engine(plan, binary)
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    for path in plan["paths"]:
        subprocess.run(["ip", "-4" if path["address_family"] == 4 else "-6", "addr", "add", path["address"]+("/32" if path["address_family"] == 4 else "/128"), "dev", "lo"], check=True)
    if subprocess.check_output(["ip", "route", "show", "default"]).strip() or subprocess.check_output(["ip", "-6", "route", "show", "default"]).strip():
        raise RuntimeError("native namespace has an unexpected external route")
    log_dir = Path("/var/log/wavemesh-node")
    if log_dir.exists(): raise RuntimeError("refusing pre-existing log ownership")
    log_dir.mkdir(mode=0o700)
    processes, servers, threads = [], [], []
    dns_socket, dns_thread, dns_stop = None, None, threading.Event()
    dns_received = []
    try:
        with tempfile.TemporaryDirectory(prefix="wm-prepared-native-") as temporary:
            directory = Path(temporary); os.chmod(directory, 0o700)
            cert, key = directory/"cert.pem", directory/"key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-subj", "/CN=fixture.example", "-addext", "subjectAltName=DNS:upcloud.example.com,DNS:infomaniak.example.com", "-days", "1"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            os.chmod(key, 0o600)
            environment = dict(os.environ)

            def origin(label, ipv6=False):
                class Handler(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.send_response(200); self.send_header("Content-Length", str(len(label.encode()))); self.end_headers(); self.wfile.write(label.encode())
                    def log_message(self, *args): pass
                class Server(http.server.ThreadingHTTPServer):
                    address_family = socket.AF_INET6 if ipv6 else socket.AF_INET
                server = Server(("::1" if ipv6 else "127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                servers.append(server); threads.append(thread)
                return server.server_address[1]

            sentinel_port, sentinel6 = origin("sentinel"), origin("sentinel6", True)
            dns_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            dns_socket.bind(("127.0.0.1", 0)); dns_socket.settimeout(.2)
            dns_port = dns_socket.getsockname()[1]
            def dns_server():
                while not dns_stop.is_set():
                    try: data, address = dns_socket.recvfrom(4096)
                    except socket.timeout: continue
                    except OSError: break
                    dns_received.append(data)
                    dns_socket.sendto(data[:2]+b"\x81\x80"+data[4:], address)
            dns_thread = threading.Thread(target=dns_server, daemon=True); dns_thread.start()
            def start(name, document):
                document = copy.deepcopy(document)
                # Native fixtures cannot possess a public CA's signing key.
                # Pin only the ephemeral fixture CA without insecure TLS or
                # disabling hostname verification. Production bytes stay intact.
                for outbound in document.get("outbounds", []):
                    stream = outbound.get("streamSettings", {})
                    if stream.get("security") == "tls":
                        stream["tlsSettings"]["certificates"] = [{"certificateFile": str(cert), "usage": "verify"}]
                        stream["tlsSettings"]["disableSystemRoot"] = True
                if diagnose:
                    document["log"] = {"access": "none", "error": str(directory/f"{name}.log"), "loglevel": "debug"}
                path = directory/f"{name}.json"; path.write_text(json.dumps(document)); os.chmod(path, 0o600)
                subprocess.run([binary, "run", "-test", "-config", str(path)], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=15)
                process = subprocess.Popen([binary, "run", "-config", str(path)], env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                processes.append(process); return process

            exits = {}
            for path in plan["paths"]:
                target = next(e for e in config["exits"] if e["id"] == path["exit_id"])
                endpoint = target["endpoint"]; port = origin(path["id"])
                document = {"log": {"loglevel": "none"}, "inbounds": [{"listen": path["address"], "port": 443, "protocol": "vless", "settings": {"clients": [{"id": endpoint["relay_uuid"]}], "decryption": "none"}, "streamSettings": {"network": "xhttp", "security": "tls", "tlsSettings": {"certificates": [{"certificateFile": str(cert), "keyFile": str(key)}]}, "xhttpSettings": {"path": endpoint["relay_path"], "host": endpoint["host"], "mode": "stream-one"}}}], "outbounds": [{"protocol": "freedom", "settings": {"redirect": f"127.0.0.1:{port}"}}]}
                # Stock server-side VLESS intentionally denies reserved targets.
                # Allow only this fixture's TCP receiver, in an isolated netns;
                # never weaken the production Exit's final outbound policy.
                document["outbounds"][0]["settings"]["finalRules"] = [{"action": "allow", "network": "tcp", "ip": ["127.0.0.1/32"], "port": f"{port},{sentinel_port}"}]
                exits[path["id"]] = start(path["id"], document)
            target_socks, sentinel_socks = free_port(), free_port()
            probes = {p["id"]: free_port() for p in plan["paths"]}
            template["inbounds"] = [{"listen": "127.0.0.1", "port": port, "tag": tag, "protocol": "socks", "settings": {"auth": "noauth", "udp": True}} for tag, port in [("wm-route-auto-europe", target_socks), ("sentinel", sentinel_socks), *[(f"probe-{p}", port) for p, port in probes.items()]]]
            template["routing"]["rules"] = [{"type": "field", "inboundTag": [f"probe-{p}"], "outboundTag": f"wm-exit-path-{p}"} for p in probes] + [{"type": "field", "inboundTag": ["sentinel"], "outboundTag": "direct"}]
            config, runtime, current, binding = channels.stage(config, {}, "route-auto-europe", plan, template, now=int(time.time()))
            entry = start("entry0", current)
            def wait_ready():
                for _ in range(50):
                    if proxy_get(sentinel_socks, "127.0.0.1", sentinel_port) == b"sentinel": return
                    if entry.poll() is not None: raise RuntimeError("native Entry engine exited")
                    time.sleep(.1)
                raise RuntimeError("native Entry listener not ready")
            def replace(document, name):
                nonlocal entry
                entry.terminate(); entry.wait(timeout=10); entry = start(name, document); wait_ready()
            wait_ready()
            assert proxy_get(target_socks, "127.0.0.1", sentinel_port) == b""
            assert proxy_get(sentinel_socks, "::1", sentinel6) == b"sentinel6"
            def tick():
                nonlocal config, runtime, current
                live = {p: proxy_get(port, "127.0.0.1", sentinel_port) == p.encode() for p, port in probes.items()}
                now = int(time.time())
                prior = runtime.get("prepared_routes", {}).get("route-auto-europe", {}).get("sample_at", 0)
                if now <= prior: time.sleep(1.01); now = int(time.time())
                observation = {"schema_version": 1, "binding": binding, "engine": engine, "at": now, "paths": live}
                config, runtime, current, _ = channels.stage(config, runtime, "route-auto-europe", plan, current, observation, now)
                replace(current, f"entry-{now}")
                return live
            first = tick()
            if not all(first.values()):
                tls = {}
                for path in plan["paths"]:
                    endpoint = next(e for e in config["exits"] if e["id"] == path["exit_id"])["endpoint"]
                    try:
                        with socket.create_connection((path["address"], 443), timeout=2) as raw:
                            with ssl.create_default_context(cafile=str(cert)).wrap_socket(raw, server_hostname=endpoint["sni"]): pass
                        tls[path["id"]] = "PASS"
                    except (OSError, ssl.SSLError) as error:
                        tls[path["id"]] = type(error).__name__
                error_log = log_dir/"xray-error.log"
                messages = error_log.read_text(errors="replace") if error_log.exists() else ""
                if diagnose:
                    messages += "\n".join(p.read_text(errors="replace") for p in directory.glob("*.log"))
                    selected = [line for line in messages.splitlines() if any(word in line.lower() for word in ("failed", "rejected", "blocked", "handshake", "error", "invalid"))]
                    for endpoint in config["exits"]:
                        for field in ("relay_uuid", "relay_path", "domain", "sni", "host"):
                            selected = [line.replace(endpoint["endpoint"][field], "<fixture-value>") for line in selected]
                    selected = [re.sub(r"/tmp/[^\s]+", "<fixture-path>", line) for line in selected]
                    print(json.dumps({"syntheticDiagnosticOnly": True, "redactedErrors": selected[-12:]}), flush=True)
                print(json.dumps({"fullPath": first, "proxyDiagnostics": {p: PROXY_DIAGNOSTICS.get(port) for p, port in probes.items()}, "directTls": tls, "errorCategories": {word: messages.lower().count(word) for word in ("x509", "certificate", "timeout", "http", "reset", "refused", "failed")}}), flush=True)
            assert all(first.values()); assert all(tick().values())
            assert proxy_get(target_socks, "127.0.0.1", sentinel_port) == b"upcloud-v4"
            exits["upcloud-v4"].terminate(); exits["upcloud-v4"].wait(timeout=10)
            tick(); tick()
            assert proxy_get(target_socks, "127.0.0.1", sentinel_port) == b"upcloud-v6"
            exits["upcloud-v6"].terminate(); exits["upcloud-v6"].wait(timeout=10)
            tick(); tick()
            assert proxy_get(target_socks, "127.0.0.1", sentinel_port) == b"infomaniak-v4"
            for process in exits.values():
                if process.poll() is None: process.terminate(); process.wait(timeout=10)
            assert not any(tick().values())
            assert proxy_get(target_socks, "127.0.0.1", sentinel_port) == b""
            assert proxy_get(target_socks, "::1", sentinel6) == b""
            assert proxy_get(sentinel_socks, "127.0.0.1", sentinel_port) == b"sentinel"
            assert proxy_dns(sentinel_socks, dns_port)
            before = len(dns_received)
            assert not proxy_dns(target_socks, dns_port)
            assert len(dns_received) == before
            assert proxy_dns(sentinel_socks, dns_port)
            print(json.dumps({"status": "PASS", "engine": engine, "scope": "isolated-Xray-XHTTP-TLS-underlay-and-routing", "fixtureCAsha256": hashlib.sha256(cert.read_bytes()).hexdigest(), "fixtureOverrides": ["explicit synthetic verifying CA, strict SNI/TLS", "Exit allow only own redirected TCP receiver", "SOCKS test ingress, production ingress not qualified"], "cases": ["initial-block", "two-exit-four-full-paths", "ipv4-to-ipv6", "other-approved-exit", "all-down-ipv4-block", "all-down-ipv6-block", "all-down-DNS-UDP-no-delivery", "sentinel-before-after"], "limitations": ["3X-UI/nginx/systemd transaction pending", "active-session preservation not claimed", "RF/provider/liveNode not qualified"]}))
    finally:
        for process in reversed(processes):
            if process.poll() is None: process.terminate()
        for process in processes: process.wait(timeout=10)
        for server in servers: server.shutdown(); server.server_close()
        for thread in threads: thread.join(timeout=3)
        dns_stop.set()
        if dns_socket is not None: dns_socket.close()
        if dns_thread is not None: dns_thread.join(timeout=3)
        for file in log_dir.iterdir(): file.unlink()
        log_dir.rmdir()


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--engine", required=True); parser.add_argument("--inside", action="store_true"); parser.add_argument("--diagnose", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0: raise SystemExit("native Linux root fixture required")
    if hashlib.sha256(Path(args.engine).read_bytes()).hexdigest() != ENGINE_SHA256: raise SystemExit("pinned native engine mismatch")
    if not args.inside:
        child = subprocess.Popen(["unshare", "--net", sys.executable, "-B", str(Path(__file__).resolve()), "--inside", "--engine", str(Path(args.engine).resolve()), *(["--diagnose"] if args.diagnose else [])], start_new_session=True)
        print(json.dumps({"nativeOwnedProcessGroup": child.pid}), flush=True)
        try:
            if child.wait(timeout=180) != 0: raise RuntimeError("native child failed")
        finally:
            try: os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            child.wait(timeout=10)
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                pass
            else:
                os.killpg(child.pid, signal.SIGKILL)
                raise RuntimeError("native child left a process-group cleanup debt")
    else:
        run(str(Path(args.engine).resolve()), args.diagnose)


if __name__ == "__main__": main()
