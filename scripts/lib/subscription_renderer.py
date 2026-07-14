#!/usr/bin/env python3
import argparse, json, os, secrets, tempfile, urllib.parse
from pathlib import Path

def atomic(path,data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); fd,tmp=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as out: json.dump(data,out,indent=2,ensure_ascii=False,sort_keys=True); out.write("\n"); out.flush(); os.fsync(out.fileno())
        os.chmod(tmp,0o600); os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
def secure_subscription_id(value,used):
    if value and not value.startswith("sub-client-") and value not in used: used.add(value); return value
    while True:
        value="sub-"+secrets.token_urlsafe(18)
        if value not in used: used.add(value); return value
def forbidden_values(cfg):
    values={"127.0.0.1","localhost",str(cfg.get("server",{}).get("public_ip","")),str(cfg.get("panel",{}).get("listen_port","")),str(cfg.get("network",{}).get("xhttp",{}).get("port",""))}
    for item in cfg.get("exits",[]):
        endpoint=item.get("endpoint",{}); values.update({str(endpoint.get("domain","")),str(endpoint.get("relay_uuid","")),str(endpoint.get("relay_path",""))}); values.update(str(x) for x in endpoint.get("expected_public_ips",[]))
    for route in cfg.get("routes",[]): values.add(str(route.get("entry",{}).get("local_port","")))
    for peer in cfg.get("relay_peers",[]): values.add(str(peer.get("inbound",{}).get("local_port","")))
    return {x for x in values if x}
def render(cfg,output_dir):
    if cfg.get("node",{}).get("role") not in ("entry","standalone"): raise ValueError("subscriptions are only generated on entry/standalone nodes")
    domain=cfg["server"]["domain"]; routes={x["id"]:x for x in cfg.get("routes",[]) if x.get("enabled",True)}; exits={x["id"]:x for x in cfg.get("exits",[]) if x.get("enabled",True)}; used=set(); metadata=[]
    output_dir=Path(output_dir); users_dir=output_dir/"users"; users_dir.mkdir(parents=True,exist_ok=True)
    for client in cfg.get("clients",[]):
        client["subscription_id"]=secure_subscription_id(client.get("subscription_id",""),used)
        if not client.get("enabled",True): continue
        profiles=[]
        credentials={x["route_id"]:x for x in client.get("credentials",[]) if x.get("enabled",True)}
        ordered=sorted((r for r in routes.values() if ((r.get("kind")=="cascade" and r.get("exit_id") in exits) or (r.get("kind")=="auto" and r.get("presentation",{}).get("published",False))) and r["id"] in credentials),key=lambda r:(r.get("sort_order",0),r.get("display_name",""),r["id"]))
        for route in ordered:
            credential=credentials[route["id"]]; path=route["entry"]["public_path"]
            if route.get("kind")=="cascade": name=route.get("display_name") or exits[route["exit_id"]].get("display_name") or route["id"]
            else: name=route.get("display_name") or route["id"]
            query=urllib.parse.urlencode({"encryption":"none","security":"tls","type":"xhttp","host":domain,"sni":domain,"fp":"randomized","path":path,"mode":"stream-one"})
            profiles.append(f"vless://{credential['uuid']}@{domain}:443?{query}#{urllib.parse.quote(name,safe='')}")
        content="\n".join(profiles)+("\n" if profiles else "")
        for forbidden in forbidden_values(cfg):
            if forbidden in content: raise ValueError("subscription contains a forbidden internal or Exit value")
        target=users_dir/f"{client['subscription_id']}.txt"; target.write_text(content,encoding="utf-8"); os.chmod(target,0o644)
        metadata.append({"client_id":client["id"],"subscription_id":client["subscription_id"],"path":f"/sub/{client['subscription_id']}/","profiles":len(profiles)})
    first=(users_dir/f"{metadata[0]['subscription_id']}.txt").read_text(encoding="utf-8") if metadata else ""; (output_dir/"sub.txt").write_text(first,encoding="utf-8"); os.chmod(output_dir/"sub.txt",0o644)
    return metadata
def main():
    p=argparse.ArgumentParser(); p.add_argument("--config",required=True); p.add_argument("--output-config",required=True); p.add_argument("--output-dir",required=True); p.add_argument("--metadata",required=True); a=p.parse_args()
    cfg=json.loads(Path(a.config).read_text(encoding="utf-8")); metadata=render(cfg,a.output_dir); atomic(a.output_config,cfg); atomic(a.metadata,metadata)
if __name__=="__main__": main()
