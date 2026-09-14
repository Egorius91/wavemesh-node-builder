"""Exercise actual nginx success and upstream-error logs with synthetic secrets."""
import http.server
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/lib"))
from nginx_renderer import render

NGINX = shutil.which("nginx")
if not NGINX and os.environ.get("WAVEMESH_REQUIRE_NGINX_TESTS") == "1":
    raise RuntimeError("nginx integration test dependency is required in CI")


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(NGINX, "nginx integration dependency not installed")
class RequestPrivacyTests(unittest.TestCase):
    def test_success_and_upstream_failure_do_not_log_subscription_credentials(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"fixture")
            def log_message(self, *_args):
                pass
        backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(backend.server_close)
        self.addCleanup(backend.shutdown)
        fixture = {"network": {"subscription": {"backend": "xui-native",
                    "path": "/private-subscription/", "local_port": backend.server_port}}}
        # Hold a bound, non-listening socket so a real failed upstream cannot race
        # another service claiming the chosen port.
        with socket.socket() as dead, tempfile.TemporaryDirectory() as folder:
            dead.bind(("127.0.0.1", 0))
            locations = render(fixture, additional_native_path="/private-broken/",
                               additional_native_port=dead.getsockname()[1])
            for protected in (False, True):
                with self.subTest(protected=protected):
                    root = Path(folder) / str(protected)
                    root.mkdir()
                    listen = port()
                    privacy = "access_log off;\nerror_log /dev/null;\n"
                    self.assertTrue(locations.startswith(privacy))
                    body = locations if protected else locations.removeprefix(privacy)
                    config = root / "nginx.conf"
                    config.write_text(f"daemon off; master_process off; pid {root}/nginx.pid;\n"
                        f"error_log {root}/error.log info;\nevents {{}}\nhttp {{\n"
                        f"client_body_temp_path {root}/body; proxy_temp_path {root}/proxy;\n"
                        f"fastcgi_temp_path {root}/fastcgi; uwsgi_temp_path {root}/uwsgi; scgi_temp_path {root}/scgi;\n"
                        f"access_log {root}/access.log combined;\nserver {{ listen 127.0.0.1:{listen};\n"
                        + body + "\n}\n}\n")
                    token = "fixture-" + "a" * 24
                    query = "fixture-" + "b" * 24
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with (root / "stderr.log").open("w") as stderr:
                        child = subprocess.Popen([NGINX, "-p", str(root), "-c", str(config)],
                            stdout=subprocess.DEVNULL, stderr=stderr)
                        try:
                            for _ in range(100):
                                if child.poll() is not None:
                                    self.fail("fixture nginx stopped before readiness")
                                try:
                                    with socket.create_connection(("127.0.0.1", listen), timeout=.1):
                                        break
                                except OSError:
                                    time.sleep(.02)
                            else:
                                self.fail("fixture nginx did not become ready")
                            for path, expected in [("private-subscription", 200), ("private-broken", 502)]:
                                url = f"http://127.0.0.1:{listen}/{path}/{token}?credential={query}"
                                try:
                                    with opener.open(url, timeout=5) as response:
                                        status = response.status
                                        response.read()
                                except urllib.error.HTTPError as error:
                                    status = error.code
                                    error.close()
                                self.assertEqual(status, expected)
                        finally:
                            child.terminate()
                            child.wait(timeout=5)
                    logs = "\n".join(p.read_text() for p in root.glob("*.log"))
                    for secret in (token, query):
                        if protected:
                            self.assertNotIn(secret, logs)
                        else:
                            self.assertIn(secret, logs, "control must demonstrate real inherited logging")


if __name__ == "__main__":
    unittest.main()
