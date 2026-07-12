#!/usr/bin/env python3
import argparse, hashlib, ipaddress, json, os, re, tempfile
from datetime import datetime, timezone
from pathlib import Path

ID=re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$"); RELAY_PATH=re.compile(r"^/(?!.*(?:\.\.|%2[fF]|\s)).{10,126}/$")
def now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
def atomic(path,data):
    path=Path(path); fd,tmp=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as out: json.dump(data,out,indent=2,ensure_ascii=False,sort_keys=True); out.write("\n"); out.flush(); os.fsync(out.fileno())
        os.chmod(tmp,0o600); os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
def digest(data):
    clean={key:value for key,value in data.items() if key!="checksum_sha256"}
    canonical=json.dumps(clean,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
def occupied_paths(cfg):
    values={cfg.get("panel",{}).get("path"),cfg.get("network",{}).get("subscription",{}).get("path"),cfg.get("network",{}).get("xhttp",{}).get("path")}
    values.update(x.get("inbound",{}).get("public_path") for x in cfg.get("relay_peers",[])); values.update(x.get("entry",{}).get("public_path") for x in cfg.get("routes",[])); return values
def create(a):
    cfg=json.loads(Path(a.config).read_text(encoding="utf-8"))
    if cfg.get("node",{}).get("role")!="exit": raise ValueError("exit peer commands require node.role=exit")
    if not ID.fullmatch(a.entry_id): raise ValueError("invalid entry id")
    if any(x["id"]==a.entry_id for x in cfg.get("relay_peers",[])): raise ValueError("relay peer already exists")
    if not RELAY_PATH.fullmatch(a.path) or a.path in occupied_paths(cfg): raise ValueError("invalid or colliding relay path")
    allowed=[]
    if a.entry_ip: ipaddress.ip_address(a.entry_ip); allowed=[a.entry_ip]
    peer={"id":a.entry_id,"display_name":a.display_name,"enabled":True,"allowed_entry_ips":allowed,"inbound":{"listen":"127.0.0.1","local_port":a.port,"public_path":a.path,"uuid":a.uuid,"inbound_id":a.inbound_id,"inbound_tag":f"wm-relay-{a.entry_id}","xhttp_mode":"stream-one"}}
    cfg.setdefault("relay_peers",[]).append(peer); atomic(a.candidate,cfg)
    node=cfg["node"]; server=cfg["server"]
    manifest={"schema_version":1,"kind":"wavemesh-exit-join","generated_at":now(),"expires_at":None,"exit":{"id":node["id"],"display_name":("%s - %s"%(node.get("country",""),node.get("city",""))).strip(" -"),"country":node.get("country",""),"city":node.get("city",""),"domain":server["domain"],"expected_public_ips":[server["public_ip"]] if server.get("public_ip") else [],"port":443,"transport":"xhttp","xhttp_mode":"stream-one","security":"tls","sni":server["domain"],"host":server["domain"],"fingerprint":"chrome","relay_path":a.path,"relay_uuid":a.uuid},"entry_constraints":{"entry_id":a.entry_id,"allowed_entry_ips":allowed}}
    manifest["checksum_sha256"]=digest(manifest)
    if digest(manifest)!=manifest["checksum_sha256"]: raise ValueError("manifest checksum self-check failed")
    atomic(a.manifest,manifest)
def remove(a):
    cfg=json.loads(Path(a.config).read_text(encoding="utf-8")); target=next((x for x in cfg.get("relay_peers",[]) if x["id"]==a.entry_id),None)
    if not target: raise ValueError("relay peer not found")
    cfg["relay_peers"]=[x for x in cfg["relay_peers"] if x["id"]!=a.entry_id]; atomic(a.candidate,cfg); print(target["inbound"]["inbound_id"])
def main():
    p=argparse.ArgumentParser(); s=p.add_subparsers(dest="cmd",required=True); c=s.add_parser("create")
    for n in ("config","candidate","manifest","entry-id","display-name","path","uuid"): c.add_argument("--"+n,required=True)
    c.add_argument("--entry-ip",default=""); c.add_argument("--port",type=int,required=True); c.add_argument("--inbound-id",type=int,required=True)
    r=s.add_parser("remove"); r.add_argument("--config",required=True); r.add_argument("--candidate",required=True); r.add_argument("--entry-id",required=True)
    a=p.parse_args(); create(a) if a.cmd=="create" else remove(a)
if __name__=="__main__": main()
