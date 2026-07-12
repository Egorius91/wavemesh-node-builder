#!/usr/bin/env python3
import argparse, hashlib, ipaddress, json, os, re, socket, ssl, tempfile, uuid
from datetime import datetime, timezone
from pathlib import Path

ID=re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$"); DOMAIN=re.compile(r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$"); SAFE_PATH=re.compile(r"^/(?!.*(?:\.\.|%2[fF]|\s)).{10,126}/$")
def load(path): return json.loads(Path(path).read_text(encoding="utf-8"))
def atomic(path,data):
    path=Path(path); fd,tmp=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as out: json.dump(data,out,indent=2,ensure_ascii=False,sort_keys=True); out.write("\n"); out.flush(); os.fsync(out.fileno())
        os.chmod(tmp,0o600); os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
def checksum(data):
    clean={k:v for k,v in data.items() if k!="checksum_sha256"}; raw=json.dumps(clean,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8"); return hashlib.sha256(raw).hexdigest()
def resolved_ips(domain): return sorted({item[4][0] for item in socket.getaddrinfo(domain,443,type=socket.SOCK_STREAM)})
def managed_id(prefix,value):
    result=prefix+value
    if len(result)<=48: return result
    return prefix+value[:48-len(prefix)-8]+"-"+hashlib.sha256(value.encode()).hexdigest()[:7]
def validate(config,manifest,allow_private=False,network=True):
    if config.get("node",{}).get("role")!="entry": raise ValueError("cascade commands require node.role=entry")
    if manifest.get("schema_version")!=1 or manifest.get("kind")!="wavemesh-exit-join": raise ValueError("unsupported manifest kind or version")
    if checksum(manifest)!=manifest.get("checksum_sha256"): raise ValueError("manifest checksum mismatch")
    exit_data=manifest.get("exit") or {}; constraints=manifest.get("entry_constraints") or {}
    if constraints.get("entry_id")!=config["node"]["id"]: raise ValueError("manifest entry constraint does not match this node")
    if not ID.fullmatch(exit_data.get("id","")) or not DOMAIN.fullmatch(exit_data.get("domain","")): raise ValueError("invalid Exit id or domain")
    uuid.UUID(exit_data.get("relay_uuid",""))
    if exit_data.get("transport")!="xhttp" or exit_data.get("xhttp_mode")!="stream-one" or exit_data.get("security")!="tls": raise ValueError("unsupported relay transport")
    if exit_data.get("sni")!=exit_data.get("domain") or exit_data.get("host")!=exit_data.get("domain") or not SAFE_PATH.fullmatch(exit_data.get("relay_path","")): raise ValueError("invalid relay TLS host or path")
    if constraints.get("allowed_entry_ips") and config.get("server",{}).get("public_ip") not in constraints["allowed_entry_ips"]: raise ValueError("Entry public IP is not allowed by manifest")
    if manifest.get("expires_at"):
        expires=datetime.fromisoformat(manifest["expires_at"].replace("Z","+00:00"));
        if expires < datetime.now(timezone.utc): raise ValueError("manifest expired")
    if network:
        addresses=resolved_ips(exit_data["domain"]); expected=exit_data.get("expected_public_ips") or []
        if expected and not set(addresses).intersection(expected): raise ValueError("Exit DNS does not match expected public IPs")
        if not allow_private and any(ipaddress.ip_address(x).is_private or ipaddress.ip_address(x).is_loopback for x in addresses): raise ValueError("private or loopback Exit target rejected")
        context=ssl.create_default_context()
        with socket.create_connection((exit_data["domain"],int(exit_data["port"])),timeout=10) as raw:
            with context.wrap_socket(raw,server_hostname=exit_data["sni"]): pass
    return exit_data
def inspect(config,manifest):
    found=next((x for x in config.get("exits",[]) if x["id"]==manifest["exit"]["id"]),None)
    if not found: return "new"
    route_id=managed_id("route-",manifest["exit"]["id"]); route=next((x for x in config.get("routes",[]) if x["id"]==route_id and x.get("exit_id")==manifest["exit"]["id"]),None)
    return "same" if found.get("manifest_checksum")==manifest.get("checksum_sha256") and route else "conflict"
def prepare(a):
    cfg=load(a.config); manifest=load(a.manifest); e=manifest["exit"]; exit_id=e["id"]; route_id=managed_id("route-",exit_id); outbound_tag=f"wm-exit-{exit_id}"; inbound_tag=f"wm-route-{exit_id}"; rule_tag=f"wm-rule-{exit_id}"
    credentials=[]; inbound_clients=[]; used={c.get("uuid") for client in cfg.get("clients",[]) for c in client.get("credentials",[])}|{e["relay_uuid"]}
    for client in cfg.get("clients",[]):
        value=str(uuid.uuid4())
        while value in used: value=str(uuid.uuid4())
        used.add(value); email=f"wm.{client['id']}.{route_id}"; credential={"route_id":route_id,"email":email,"uuid":value,"enabled":True}; client.setdefault("credentials",[]).append(credential); credentials.append(credential)
        inbound_clients.append({"id":value,"email":email,"enable":True,"flow":"","limitIp":0,"totalGB":0,"expiryTime":0,"tgId":"","subId":client.get("subscription_id","")})
    if not credentials: raise ValueError("Entry has no clients")
    cfg.setdefault("exits",[]).append({"id":exit_id,"display_name":a.display_name or e["display_name"],"country":e["country"],"city":e["city"],"enabled":True,"manifest_checksum":manifest["checksum_sha256"],"endpoint":{"domain":e["domain"],"expected_public_ips":e.get("expected_public_ips",[]),"port":e["port"],"security":"tls","sni":e["sni"],"host":e["host"],"fingerprint":e["fingerprint"],"transport":"xhttp","xhttp_mode":"stream-one","relay_path":e["relay_path"],"relay_uuid":e["relay_uuid"]},"xray":{"outbound_tag":outbound_tag}})
    cfg.setdefault("routes",[]).append({"id":route_id,"kind":"cascade","display_name":a.display_name or e["display_name"],"exit_id":exit_id,"enabled":True,"sort_order":a.sort_order,"entry":{"listen":"127.0.0.1","local_port":a.port,"public_path":a.path,"inbound_id":None,"inbound_tag":inbound_tag,"xhttp_mode":"stream-one"},"routing":{"rule_tag":rule_tag,"outbound_tag":outbound_tag}})
    outbound={"tag":outbound_tag,"protocol":"vless","settings":{"vnext":[{"address":e["domain"],"port":e["port"],"users":[{"id":e["relay_uuid"],"encryption":"none"}]}]},"streamSettings":{"network":"xhttp","security":"tls","tlsSettings":{"serverName":e["sni"],"allowInsecure":False,"fingerprint":e["fingerprint"]},"xhttpSettings":{"path":e["relay_path"],"host":e["host"],"mode":"stream-one"}}}
    atomic(a.candidate,cfg); atomic(a.clients,inbound_clients); atomic(a.outbound,outbound)
def finalize(a):
    cfg=load(a.candidate); route=next(x for x in cfg["routes"] if x["id"]==a.route_id); route["entry"]["inbound_id"]=a.inbound_id; atomic(a.candidate,cfg)
def main():
    p=argparse.ArgumentParser(); s=p.add_subparsers(dest="cmd",required=True)
    v=s.add_parser("validate"); v.add_argument("--config",required=True); v.add_argument("--manifest",required=True); v.add_argument("--allow-private",action="store_true"); v.add_argument("--skip-network",action="store_true")
    i=s.add_parser("inspect"); i.add_argument("--config",required=True); i.add_argument("--manifest",required=True)
    q=s.add_parser("prepare")
    for n in ("config","manifest","candidate","clients","outbound","path"): q.add_argument("--"+n,required=True)
    q.add_argument("--port",type=int,required=True); q.add_argument("--sort-order",type=int,default=100); q.add_argument("--display-name",default="")
    f=s.add_parser("finalize"); f.add_argument("--candidate",required=True); f.add_argument("--route-id",required=True); f.add_argument("--inbound-id",type=int,required=True)
    a=p.parse_args()
    if a.cmd=="validate": validate(load(a.config),load(a.manifest),a.allow_private,not a.skip_network)
    elif a.cmd=="inspect": print(inspect(load(a.config),load(a.manifest)))
    elif a.cmd=="prepare": prepare(a)
    else: finalize(a)
if __name__=="__main__": main()
