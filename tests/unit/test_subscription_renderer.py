import json, subprocess, sys, tempfile
from pathlib import Path

root=Path(__file__).resolve().parents[2]; exit_tool=root/"scripts/lib/exit_peer.py"; cascade=root/"scripts/lib/cascade.py"; renderer=root/"scripts/lib/subscription_renderer.py"; nginx_renderer=root/"scripts/lib/nginx_renderer.py"
with tempfile.TemporaryDirectory() as name:
    temp=Path(name); manifest=temp/"join.json"; exit_candidate=temp/"exit.json"; candidate=temp/"entry.json"; final=temp/"final.json"; clients=temp/"clients.json"; outbound=temp/"outbound.json"; output=temp/"subscriptions"; metadata=temp/"metadata.json"; nginx=temp/"nginx.conf"
    subprocess.run([sys.executable,str(exit_tool),"create","--config",str(root/"tests/fixtures/config-exit-v2.json"),"--candidate",str(exit_candidate),"--manifest",str(manifest),"--entry-id","ru-msk-1","--entry-ip","203.0.113.10","--display-name","Entry","--path","/relay/example-secret/","--uuid","00000000-0000-0000-0000-000000000002","--port","22001","--inbound-id","8"],check=True)
    subprocess.run([sys.executable,str(cascade),"prepare","--config",str(root/"tests/fixtures/config-entry-v2.json"),"--manifest",str(manifest),"--candidate",str(candidate),"--clients",str(clients),"--outbound",str(outbound),"--path","/api/de/example-route/","--port","21001","--display-name","RU -> Germany"],check=True)
    subprocess.run([sys.executable,str(cascade),"finalize","--candidate",str(candidate),"--route-id","route-de-fra-1","--inbound-id","12"],check=True)
    subprocess.run([sys.executable,str(renderer),"--config",str(candidate),"--output-config",str(final),"--output-dir",str(output),"--metadata",str(metadata)],check=True)
    cfg=json.loads(final.read_text()); sub_id=cfg["clients"][0]["subscription_id"]; assert sub_id.startswith("sub-") and sub_id!="sub-client-1"
    content=(output/"users"/f"{sub_id}.txt").read_text(); assert content.startswith("vless://") and "@ru-entry.example.com:443" in content and "path=%2Fapi%2Fde%2Fexample-route%2F" in content
    for forbidden in ("de-exit.example.com","198.51.100.20","00000000-0000-0000-0000-000000000002","/relay/example-secret/","21001","22001","127.0.0.1"): assert forbidden not in content
    expected_path=cfg["network"]["subscription"]["path"]
    subprocess.run([sys.executable,str(nginx_renderer),"--config",str(final),"--output",str(nginx)],check=True); rendered=nginx.read_text(); assert f"location = {expected_path}" in rendered and "/sub/" not in rendered and "root /var/www/wavemesh-sub/users;" in rendered and f"try_files /{sub_id}.txt =404;" in rendered and 'Profile-Title "base64:V2F2ZU1lc2hWUE4="' in rendered and "alias " not in rendered
    item=json.loads(metadata.read_text())[0]; assert item["profiles"]==1 and item["path"]==expected_path
print("subscription renderer tests: OK")
