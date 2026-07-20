import hashlib, json, subprocess, sys, tempfile
from pathlib import Path

root=Path(__file__).resolve().parents[2]; tool=root/"scripts/lib/exit_peer.py"; renderer=root/"scripts/lib/nginx_renderer.py"; fixture=root/"tests/fixtures/config-exit-v2.json"
stream_timeouts=("proxy_connect_timeout 10s;","proxy_send_timeout 300s;","proxy_read_timeout 300s;","send_timeout 300s;")
with tempfile.TemporaryDirectory() as name:
    temp=Path(name); candidate=temp/"candidate.json"; manifest=temp/"peer.join.json"; nginx=temp/"managed.conf"; removed=temp/"removed.json"
    subprocess.run([sys.executable,str(tool),"create","--config",str(fixture),"--candidate",str(candidate),"--manifest",str(manifest),"--entry-id","ru-msk-1","--entry-ip","203.0.113.10","--display-name","RU Moscow Entry","--path","/relay/example-secret/","--uuid","00000000-0000-0000-0000-000000000002","--port","22001","--inbound-id","8"],check=True)
    cfg=json.loads(candidate.read_text()); peer=cfg["relay_peers"][0]; assert peer["inbound"]["listen"]=="127.0.0.1" and peer["allowed_entry_ips"]==["203.0.113.10"]
    data=json.loads(manifest.read_text()); checksum=data.pop("checksum_sha256"); raw=json.dumps(data,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode(); assert checksum==hashlib.sha256(raw).hexdigest()
    subprocess.run([sys.executable,str(renderer),"--config",str(candidate),"--output",str(nginx)],check=True); rendered=nginx.read_text(); assert "allow 203.0.113.10;" in rendered and "deny all;" in rendered and "127.0.0.1:22001" in rendered; assert all(directive in rendered for directive in stream_timeouts); assert "00000000" not in rendered
    inbound_id=subprocess.check_output([sys.executable,str(tool),"remove","--config",str(candidate),"--candidate",str(removed),"--entry-id","ru-msk-1"],text=True).strip(); assert inbound_id=="8" and json.loads(removed.read_text())["relay_peers"]==[]
print("exit peer tests: OK")
