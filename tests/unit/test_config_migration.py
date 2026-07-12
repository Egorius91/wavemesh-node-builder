import json, shutil, stat, subprocess, sys, tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
tool = root / "scripts" / "lib" / "config_json.py"
fixture = root / "tests" / "fixtures" / "config-v1.json"
with tempfile.TemporaryDirectory() as name:
    temp = Path(name); config = temp / "config.json"; backups = temp / "backups"
    shutil.copy2(fixture, config)
    subprocess.run([sys.executable, str(tool), "migrate", str(config), str(backups)], check=True)
    migrated = json.loads(config.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 2 and migrated["node"]["role"] == "standalone"
    assert migrated["network"]["subscription"]["path"] == "/sub/legacy-path/"
    assert migrated["clients"][0]["credentials"][0]["uuid"] == "00000000-0000-0000-0000-000000000001"
    assert migrated["routes"][0]["id"] == "route-standalone-default"
    assert len(migrated["migrations"]) == 1
    if sys.platform != "win32":
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert len(list(backups.iterdir())) == 1
    subprocess.run([sys.executable, str(tool), "migrate", str(config), str(backups)], check=True)
    assert len(list(backups.iterdir())) == 1
print("config migration tests: OK")
