import importlib.util
import json
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]

path_spec = importlib.util.spec_from_file_location(
    "subscription_path", root / "scripts/lib/subscription_path.py"
)
path_module = importlib.util.module_from_spec(path_spec)
path_spec.loader.exec_module(path_module)

renderer_spec = importlib.util.spec_from_file_location(
    "subscription_renderer", root / "scripts/lib/subscription_renderer.py"
)
renderer = importlib.util.module_from_spec(renderer_spec)
renderer_spec.loader.exec_module(renderer)

with tempfile.TemporaryDirectory() as directory:
    base = Path(directory)
    original = base / "config.json"
    rotated = base / "rotated.json"
    config = {
        "node": {"role": "entry"},
        "server": {"domain": "entry.example.com"},
        "network": {
            "subscription": {"path": "/sub/legacy-token/"},
            "xhttp": {"port": 12345},
        },
        "panel": {"listen_port": 54321},
        "clients": [],
        "routes": [],
        "exits": [],
    }
    original.write_text(json.dumps(config), encoding="utf-8")

    path_module.rotate(original, rotated)
    updated = json.loads(rotated.read_text(encoding="utf-8"))
    new_path = updated["network"]["subscription"]["path"]
    assert new_path.startswith("/") and new_path.endswith("/")
    assert not new_path.startswith("/sub/")
    assert len([part for part in new_path.split("/") if part]) == 2

    updated["clients"] = [
        {
            "id": "client-1",
            "enabled": True,
            "subscription_id": "opaque-client-id",
            "credentials": [],
        }
    ]
    output = base / "subscriptions"
    metadata = renderer.render(updated, output)
    assert metadata[0]["path"] == new_path
    assert "/sub/" not in metadata[0]["path"]

print("subscription path tests: OK")
