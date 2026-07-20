import importlib.util
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

STREAM_TIMEOUTS = (
    "proxy_connect_timeout 10s;",
    "proxy_send_timeout 300s;",
    "proxy_read_timeout 300s;",
    "send_timeout 300s;",
)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


subscription = load_module("subscription_renderer", ROOT / "scripts/lib/subscription_renderer.py")
nginx = load_module("nginx_renderer", ROOT / "scripts/lib/nginx_renderer.py")
auto = load_module("auto_route", ROOT / "scripts/lib/auto_route.py")


def config(published=False):
    return {
        "node": {"role": "entry"},
        "server": {"domain": "entry.example.com", "public_ip": "198.51.100.10"},
        "panel": {"listen_port": 2053, "path": "/panel/"},
        "network": {"subscription": {"path": "/sub/"}, "xhttp": {"port": 21000, "path": "/base/"}},
        "relay_peers": [],
        "exits": [{"id": "de-1", "enabled": True, "endpoint": {"domain": "exit.example.com", "relay_uuid": "relay-secret", "relay_path": "/relay/secret/", "expected_public_ips": ["203.0.113.20"]}, "xray": {"outbound_tag": "wm-exit-de-1"}}],
        "balancers": [{"id": "auto-europe", "enabled": True, "strategy": "leastPing", "selector": ["wm-exit-de-1"], "balancer_tag": "wm-balancer-auto-europe"}],
        "routes": [
            {"id": "route-de-1", "kind": "cascade", "display_name": "Germany", "exit_id": "de-1", "enabled": True, "sort_order": 100, "entry": {"public_path": "/api/de/abcdefghijkl/", "local_port": 21101}},
            {"id": "route-auto-auto-europe", "kind": "auto", "display_name": "⚡ RU -> Auto Europe", "enabled": True, "sort_order": 50, "balancer_id": "auto-europe", "entry": {"public_path": "/api/auto/abcdefghijkl/", "local_port": 21102}, "presentation": {"published": published}},
        ],
        "clients": [{"id": "client-1", "enabled": True, "subscription_id": "ABCDEFGHIJKLMNOP", "credentials": [
            {"route_id": "route-de-1", "uuid": "11111111-1111-4111-8111-111111111111", "enabled": True},
            {"route_id": "route-auto-auto-europe", "uuid": "22222222-2222-4222-8222-222222222222", "enabled": True},
        ]}],
    }


def test_unpublished_auto_is_hidden():
    cfg = config(False)
    with tempfile.TemporaryDirectory() as directory:
        subscription.render(cfg, directory)
        content = Path(directory, "users", "ABCDEFGHIJKLMNOP.txt").read_text()
    assert "Auto%20Europe" not in content
    assert "/api/auto/" not in nginx.render(cfg)


def test_published_auto_is_public_without_exit_secrets():
    cfg = config(True)
    with tempfile.TemporaryDirectory() as directory:
        metadata = subscription.render(cfg, directory)
        content = Path(directory, "users", "ABCDEFGHIJKLMNOP.txt").read_text()
    assert metadata[0]["profiles"] == 2
    assert "22222222-2222-4222-8222-222222222222@entry.example.com:443" in content
    assert "%E2%9A%A1%20RU%20-%3E%20Auto%20Europe" in content
    for forbidden in ("exit.example.com", "relay-secret", "/relay/secret/", "203.0.113.20", "21102"):
        assert forbidden not in content
    rendered = nginx.render(cfg)
    assert "location /api/auto/abcdefghijkl/" in rendered
    assert "proxy_pass http://127.0.0.1:21102;" in rendered
    for directive in STREAM_TIMEOUTS:
        assert directive in rendered


def test_disabled_auto_cannot_be_published():
    cfg = config(False)
    cfg["routes"][1]["enabled"] = False
    try:
        auto.set_published(cfg, "auto-europe", True)
    except ValueError as error:
        assert "disabled" in str(error)
    else:
        raise AssertionError("disabled Auto Route was published")


def test_unpublish_removes_only_auto_profile():
    cfg = auto.set_published(config(True), "auto-europe", False)
    with tempfile.TemporaryDirectory() as directory:
        subscription.render(cfg, directory)
        content = Path(directory, "users", "ABCDEFGHIJKLMNOP.txt").read_text()
    assert "Germany" in content
    assert "Auto%20Europe" not in content
