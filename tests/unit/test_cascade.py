import importlib.util, json, subprocess, sys, tempfile
from pathlib import Path

root=Path(__file__).resolve().parents[2]; exit_tool=root/"scripts/lib/exit_peer.py"; tool=root/"scripts/lib/cascade.py"; renderer=root/"scripts/lib/nginx_renderer.py"
spec=importlib.util.spec_from_file_location("cascade_tool",tool); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
assert len(module.managed_id("route-","x"*48))<=48 and module.managed_id("route-","x"*48)==module.managed_id("route-","x"*48)
exit_cfg=root/"tests/fixtures/config-exit-v2.json"; entry_cfg=root/"tests/fixtures/config-entry-v2.json"
with tempfile.TemporaryDirectory() as name:
    temp=Path(name); exit_candidate=temp/"exit.json"; manifest=temp/"join.json"; candidate=temp/"entry.json"; clients=temp/"clients.json"; outbound=temp/"outbound.json"; nginx=temp/"nginx.conf"
    subprocess.run([sys.executable,str(exit_tool),"create","--config",str(exit_cfg),"--candidate",str(exit_candidate),"--manifest",str(manifest),"--entry-id","ru-msk-1","--entry-ip","203.0.113.10","--display-name","RU Moscow","--path","/relay/example-secret/","--uuid","00000000-0000-0000-0000-000000000002","--port","22001","--inbound-id","8"],check=True)
    subprocess.run([sys.executable,str(tool),"validate","--config",str(entry_cfg),"--manifest",str(manifest),"--skip-network"],check=True)
    assert subprocess.check_output([sys.executable,str(tool),"inspect","--config",str(entry_cfg),"--manifest",str(manifest)],text=True).strip()=="new"
    subprocess.run([sys.executable,str(tool),"prepare","--config",str(entry_cfg),"--manifest",str(manifest),"--candidate",str(candidate),"--clients",str(clients),"--outbound",str(outbound),"--path","/api/de/example-route/","--port","21001","--sort-order","100"],check=True)
    subprocess.run([sys.executable,str(tool),"finalize","--candidate",str(candidate),"--route-id","route-de-fra-1","--inbound-id","12"],check=True)
    cfg=json.loads(candidate.read_text()); route=cfg["routes"][0]; assert route["entry"]["inbound_id"]==12 and route["routing"]["outbound_tag"]=="wm-exit-de-fra-1"
    credential=cfg["clients"][0]["credentials"][0]; assert credential["uuid"]!="00000000-0000-0000-0000-000000000002"
    inbound_client=json.loads(clients.read_text())[0]; assert inbound_client["tgId"] == 0 and isinstance(inbound_client["tgId"], int)
    out=json.loads(outbound.read_text()); tls=out["streamSettings"]["tlsSettings"]; assert tls["allowInsecure"] is False and tls["serverName"]=="de-exit.example.com"
    assert subprocess.check_output([sys.executable,str(tool),"inspect","--config",str(candidate),"--manifest",str(manifest)],text=True).strip()=="same"
    subprocess.run([sys.executable,str(renderer),"--config",str(candidate),"--output",str(nginx)],check=True); text=nginx.read_text(); assert "/api/de/example-route/" in text and "127.0.0.1:21001" in text; assert "de-exit.example.com" not in text and "00000000" not in text
print("cascade tests: OK")
