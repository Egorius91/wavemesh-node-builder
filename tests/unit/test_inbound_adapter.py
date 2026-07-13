import json, subprocess, sys, tempfile
from pathlib import Path

root=Path(__file__).resolve().parents[2]; tool=root/"scripts/lib/inbound_adapter.py"; clients=root/"tests/fixtures/inbound-clients.json"
with tempfile.TemporaryDirectory() as name:
    desired=Path(name)/"desired.json"
    subprocess.run([sys.executable,str(tool),"build","--tag","wm-route-de-fra-1","--remark","RU -> Germany","--port","21001","--path","/api/de/example-path/","--host","entry.example.com","--clients",str(clients),"--public-domain","entry.example.com","--output",str(desired)],check=True)
    data=json.loads(desired.read_text()); assert data["listen"]=="127.0.0.1"; assert data["streamSettings"]["xhttpSettings"]["mode"]=="stream-one"
    assert data["tag"]=="wm-route-de-fra-1" and data["remark"]=="wm-route-de-fra-1"
    assert data["streamSettings"]["externalProxy"][0]["remark"]=="RU -> Germany"
    client=data["settings"]["clients"][0]; assert client["tgId"] == 0 and isinstance(client["tgId"], int)
    empty=Path(name)/"empty.json"; empty.write_text('{"success":true,"obj":[]}')
    plan=json.loads(subprocess.check_output([sys.executable,str(tool),"plan","--desired",str(desired),"--actual",str(empty)])); assert plan["action"]=="add"
    actual=Path(name)/"actual.json"; clone=dict(data); clone["id"]=12; actual.write_text(json.dumps({"success":True,"obj":[clone]}))
    plan=json.loads(subprocess.check_output([sys.executable,str(tool),"plan","--desired",str(desired),"--actual",str(actual)])); assert plan=={"action":"noop","id":12}
    clone["streamSettings"]["externalProxy"][0]["remark"]="wm-route-de-fra-1"; actual.write_text(json.dumps({"success":True,"obj":[clone]}))
    plan=json.loads(subprocess.check_output([sys.executable,str(tool),"plan","--desired",str(desired),"--actual",str(actual)])); assert plan=={"action":"update","id":12}
    clone=json.loads(desired.read_text()); clone["id"]=12
    clone["port"]=21002; actual.write_text(json.dumps({"success":True,"obj":[clone]}))
    plan=json.loads(subprocess.check_output([sys.executable,str(tool),"plan","--desired",str(desired),"--actual",str(actual)])); assert plan=={"action":"update","id":12}
print("inbound adapter tests: OK")
