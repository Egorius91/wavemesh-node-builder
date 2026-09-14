#!/usr/bin/env python3
"""Run a packaged candidate and real Xray in a disposable CI network namespace.

No host installation, external destination, raw logs or credentials are exported.
This proves loopback candidate behavior, not staging or commercial acceptance.
"""
import argparse
from contextlib import redirect_stdout
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from urllib import error, request
import uuid

import xui_artifact

BODY = b"wavemesh-private-loopback-control"
PORT = 31000
INBOUND_PORT = 31001
TARGET_PORT = 31002
PROXIES = {"control": 31003, "candidate": 31004}


class SmokeFailure(RuntimeError):
    """Only fixed, source-defined failure codes may be exported."""


def require(condition, code):
    if not condition:
        raise SmokeFailure(code)


def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=20, **kwargs)
    require(result.returncode == 0, "CHILD_COMMAND_FAILED")
    return result.stdout


def namespace():
    require(sys.platform == "linux" and os.geteuid() == 0, "LINUX_NAMESPACE_REQUIRED")
    parent = os.environ.get("WAVEMESH_CI_PARENT_NETNS")
    require(parent and os.readlink("/proc/self/ns/net") != parent, "PRIVATE_NETWORK_REQUIRED")
    links = json.loads(command(["ip", "-j", "link"]))
    require([row["ifname"] for row in links] == ["lo"], "LOOPBACK_ONLY_REQUIRED")
    command(["ip", "link", "set", "lo", "up"])
    for family in ("-4", "-6"):
        routes = json.loads(command(["ip", family, "-j", "route", "show", "table", "all"]))
        require(all(row.get("dev") == "lo" for row in routes), "EXTERNAL_ROUTE_FORBIDDEN")


class Target(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)


class Smoke:
    def __init__(self, bundle, head, root):
        self.root = root
        self.processes = []
        self.logs = []
        self.panel = None
        self.stage = "CANDIDATE_VERIFY"
        self.checks = {}
        with redirect_stdout(io.StringIO()):
            xui_artifact.verify(bundle, head)
        self.manifest = json.loads((bundle / "manifest.json").read_text())
        # Verification above checks complete allow-listed regular-file inventory.
        # Extraction below never invokes tar's link/owner/path restoration logic.
        with tarfile.open(bundle / "x-ui-linux-amd64-wavemesh.tar.gz", "r:gz") as archive:
            for member in archive:
                target = root / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, target.open("xb") as output:
                        while block := source.read(1024 * 1024):
                            output.write(block)
                    target.chmod(member.mode)
        self.home = root / "x-ui"
        self.binary = self.home / "x-ui"
        self.xray = self.home / "bin/xray-linux-amd64"
        self.dbdir = root / "database"
        self.dbdir.mkdir(mode=0o700)
        self.db = self.dbdir / "x-ui.db"
        self.env = {"PATH": os.environ["PATH"], "HOME": str(root), "LANG": "C.UTF-8",
                    "XUI_DB_FOLDER": str(self.dbdir), "XUI_BIN_FOLDER": str(self.home / "bin"),
                    "XUI_LOG_FOLDER": str(root / "logs"), "XUI_LOG_LEVEL": "error"}
        self.username = "wm-smoke-" + secrets.token_hex(8)
        self.password = secrets.token_urlsafe(24)
        self.clients = {name: {"id": str(uuid.uuid4()), "email": "wm-" + name,
                        "subId": secrets.token_hex(12), "enable": name == "control",
                        "expiryTime": 4102444800000, "totalGB": 0, "limitIp": 0,
                        "flow": "", "reset": 0} for name in PROXIES}
        self.csrf = ""
        self.browser = request.build_opener(request.ProxyHandler({}), request.HTTPCookieProcessor(CookieJar()))

    def mark(self, name):
        self.checks[name] = True
        print(f"{name}=PASS", flush=True)

    def start(self, args, name):
        log = (self.root / (name + ".private.log")).open("ab")
        self.logs.append(log)
        child = subprocess.Popen(args, cwd=self.home, env=self.env, stdout=log, stderr=log,
                                 start_new_session=True)
        self.processes.append(child)
        return child

    @staticmethod
    def stop(child):
        # Terminate the whole private process group, including panel-owned Xray.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)

    def close(self):
        for child in reversed(self.processes):
            self.stop(child)
        for log in self.logs:
            log.close()

    def http(self, path, payload=None, csrf=True, browser=None):
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
        data = json.dumps(payload).encode() if payload is not None else None
        req = request.Request(f"http://127.0.0.1:{PORT}/smoke/" + path, data=data, headers=headers)
        with (browser or self.browser).open(req, timeout=3) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            require(len(raw) <= 4 * 1024 * 1024, "HTTP_RESPONSE_LIMIT")
            return raw

    def api(self, path, payload=None):
        reply = json.loads(self.http("panel/api/" + path, payload))
        require(reply.get("success") is True, "API_REJECTED")
        return reply.get("obj")

    def ready(self):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            require(self.panel.poll() is None, "PANEL_EXITED")
            try:
                self.csrf = json.loads(self.http("csrf-token"))["obj"]
                return
            except (error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.2)
        raise SmokeFailure("PANEL_START_TIMEOUT")

    def login(self):
        self.ready()
        reply = json.loads(self.http("login", {"username": self.username, "password": self.password}))
        require(reply.get("success") is True, "LOGIN_REJECTED")
        self.csrf = json.loads(self.http("csrf-token"))["obj"]

    def bootstrap(self):
        self.stage = "BOOTSTRAP"
        command([str(self.binary), "setting", "-port", str(PORT), "-listenIP", "127.0.0.1",
                 "-webBasePath", "/smoke/", "-username", self.username, "-password", self.password],
                cwd=self.home, env=self.env)
        # Only disposable bootstrap settings are written directly. Client creation,
        # activation and disable use authenticated HTTP; SQLite is then read-only.
        template = {"log": {"access": "none", "error": "none", "loglevel": "none"},
                    "api": {"tag": "api", "services": ["HandlerService", "LoggerService", "StatsService", "RoutingService"]},
                    "inbounds": [{"tag": "api", "listen": "127.0.0.1", "port": 62789,
                                  "protocol": "tunnel", "settings": {"rewriteAddress": "127.0.0.1"}}],
                    "outbounds": [{"tag": "direct", "protocol": "freedom", "settings": {}}],
                    "routing": {"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]},
                    "policy": {"levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}}}, "stats": {}}
        with sqlite3.connect(self.db) as db:
            for key, value in {"subEnable": "false", "subJsonEnable": "false", "subClashEnable": "false",
                               "xrayTemplateConfig": json.dumps(template)}.items():
                db.execute("DELETE FROM settings WHERE key=?", (key,))
                db.execute("INSERT INTO settings(key,value) VALUES (?,?)", (key, value))
        self.panel = self.start([str(self.binary)], "panel")
        self.ready()
        self.stage = "HTTP_AUTH_FRONTEND"
        login_html = self.http("").decode()
        assets = re.findall(r'(?:src|href)="([^"]+\.js)"', login_html)
        require(assets, "FRONTEND_ASSET_MISSING")
        asset = assets[0]
        require(not asset.startswith(("http:", "https:", "//")), "REMOTE_FRONTEND_ASSET")
        relative = asset.removeprefix("/smoke/").lstrip("/")
        javascript = self.http(relative)
        require(len(javascript) > 1024, "FRONTEND_ASSET_EMPTY")
        require(xui_artifact.digest(javascript) in self.manifest["frontend"].values(), "FRONTEND_ASSET_MISMATCH")
        anonymous = request.build_opener(request.ProxyHandler({}))
        try:
            self.http("panel/api/clients/addDisabled", {"client": self.clients["candidate"], "inboundIds": [1]}, browser=anonymous)
        except error.HTTPError as exc:
            require(exc.code in (401, 403, 404), "AUTH_REJECTION_STATUS")
        else:
            raise SmokeFailure("UNAUTHENTICATED_WRITE_ALLOWED")
        self.login()
        self.mark("REAL_HTTP_AUTH_AND_FRONTEND")

    def db_state(self, enabled):
        with sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute("SELECT id,email,uuid,enable FROM clients ORDER BY email").fetchall()
            require(len(rows) == 2, "DUPLICATE_OR_MISSING_CLIENT")
            for name, client in self.clients.items():
                matches = [row for row in rows if row[1] == client["email"]]
                require(len(matches) == 1 and matches[0][2] == client["id"]
                        and bool(matches[0][3]) == (True if name == "control" else enabled), "CLIENT_STATE_MISMATCH")
            require(db.execute("SELECT count(*) FROM client_inbounds").fetchone()[0] == 2, "DUPLICATE_BINDING")
            return [(row[0], row[1], row[2]) for row in rows]

    def setup_clients(self):
        self.stage = "HTTP_CLIENT_CREATION"
        inbound = self.api("inbounds/add", {"enable": True, "listen": "127.0.0.1", "port": INBOUND_PORT,
                           "protocol": "vless", "remark": "private-smoke",
                           "settings": json.dumps({"clients": [], "decryption": "none"}),
                           "streamSettings": json.dumps({"network": "tcp", "security": "none"}),
                           "sniffing": json.dumps({"enabled": False})})
        self.inbound_id = inbound["id"]
        for name in PROXIES:
            self.api("clients/" + ("add" if name == "control" else "addDisabled"),
                     {"client": self.clients[name], "inboundIds": [self.inbound_id]})
        self.identities = self.db_state(False)
        duplicate = json.loads(self.http("panel/api/clients/addDisabled",
                               {"client": self.clients["candidate"], "inboundIds": [self.inbound_id]}))
        require(duplicate.get("success") is False, "DUPLICATE_CREATE_NOT_REJECTED")
        require(self.db_state(False) == self.identities, "REPLAY_CHANGED_IDENTITY")
        self.mark("HTTP_DISABLED_CREATE_AND_DUPLICATE_REJECTION")
        config = {"log": {"loglevel": "none"}, "inbounds": [], "outbounds": [], "routing": {"rules": []}}
        for name, port in PROXIES.items():
            config["inbounds"].append({"tag": name, "listen": "127.0.0.1", "port": port,
                                       "protocol": "socks", "settings": {"auth": "noauth", "udp": False}})
            config["outbounds"].append({"tag": name, "protocol": "vless", "settings": {
                "vnext": [{"address": "127.0.0.1", "port": INBOUND_PORT,
                           "users": [{"id": self.clients[name]["id"], "encryption": "none"}]}]},
                "streamSettings": {"network": "tcp", "security": "none"}})
            config["routing"]["rules"].append({"type": "field", "inboundTag": [name], "outboundTag": name})
        path = self.root / "client.json"
        path.write_text(json.dumps(config))
        self.start([str(self.xray), "run", "-config", str(path)], "client")

    def traffic(self, name):
        path = "/" + secrets.token_hex(12)
        result = subprocess.run(["curl", "--silent", "--max-time", "2", "--noproxy", "",
                                 "--socks5-hostname", f"127.0.0.1:{PROXIES[name]}",
                                 f"http://127.0.0.1:{TARGET_PORT}{path}"],
                                capture_output=True, timeout=4, env=self.env)
        return result.returncode == 0 and result.stdout == BODY and path in self.target.seen

    def wait_traffic(self, name, expected):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.traffic(name) is expected:
                return
            time.sleep(0.2)
        raise SmokeFailure("VPN_TRAFFIC_EXPECTATION_FAILED")

    def denied_with_control(self):
        self.wait_traffic("control", True)
        for _ in range(3):
            require(not self.traffic("candidate"), "DISABLED_CLIENT_CONNECTED")
        require(self.traffic("control"), "HEALTHY_CONTROL_LOST")

    def run(self):
        self.bootstrap()
        self.setup_clients()
        self.target = ThreadingHTTPServer(("127.0.0.1", TARGET_PORT), Target)
        self.target.seen = []
        thread = threading.Thread(target=self.target.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            self.stage = "DISABLED_REAL_VLESS"
            self.denied_with_control()
            self.mark("DISABLED_VLESS_REJECTED_WITH_HEALTHY_CONTROL")
            self.stage = "PANEL_RESTART_PERSISTENCE"
            self.stop(self.panel)
            self.processes.remove(self.panel)
            self.panel = self.start([str(self.binary)], "panel")
            self.login()
            require(self.db_state(False) == self.identities, "RESTART_CHANGED_IDENTITY")
            self.denied_with_control()
            self.mark("DISABLED_PERSISTENCE_AFTER_PANEL_RESTART")
            self.stage = "ACTIVATION_AND_DISABLE"
            self.api("clients/update/" + self.clients["candidate"]["email"], {**self.clients["candidate"], "enable": True})
            self.wait_traffic("candidate", True)
            require(self.db_state(True) == self.identities, "ACTIVATION_CHANGED_IDENTITY")
            require(self.traffic("control"), "ACTIVATION_BROKE_CONTROL")
            self.api("clients/update/" + self.clients["candidate"]["email"], self.clients["candidate"])
            self.wait_traffic("candidate", False)
            self.denied_with_control()
            require(self.db_state(False) == self.identities, "DISABLE_CHANGED_IDENTITY")
            self.mark("ACTIVATE_DISABLE_SAME_IDENTITY_REAL_VLESS")
        finally:
            self.target.shutdown()
            self.target.server_close()
            thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--head", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = {"schema": 1, "status": "FAILED", "builder_commit": args.head,
              "scope": "PRIVATE_LOOPBACK_CI_ONLY", "deployment": "NONE", "checks": {}}
    smoke = None
    old_umask = os.umask(0o077)
    try:
        namespace()
        with tempfile.TemporaryDirectory(prefix="wm-panel-smoke-") as directory:
            try:
                smoke = Smoke(args.candidate.resolve(), args.head, Path(directory))
                smoke.run()
                report.update(status="PASS", checks=smoke.checks,
                              archive_sha256=smoke.manifest["archive_sha256"],
                              panel_sha256=smoke.manifest["members"]["x-ui/x-ui"]["sha256"],
                              xray_sha256=smoke.manifest["members"]["x-ui/bin/xray-linux-amd64"]["sha256"])
            finally:
                if smoke:
                    smoke.close()
    except Exception as exc:
        # Fixed stage/type only: provider/panel/HTTP errors can contain tokens.
        report.update(stage=smoke.stage if smoke else "ISOLATION_OR_ARTIFACT",
                      error_type=type(exc).__name__)
        if isinstance(exc, SmokeFailure):
            report["error_code"] = str(exc)
        print("PANEL_RUNTIME_SMOKE=FAILED; NO_RAW_ERROR", file=sys.stderr)
    finally:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        args.report.chmod(0o644)  # This redacted report is the only exported file.
        os.umask(old_umask)
    print(json.dumps(report))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
