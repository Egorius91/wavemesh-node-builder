#!/usr/bin/env python3
import argparse, ipaddress, json, re
from pathlib import Path

PATH_RE=re.compile(r"^/(?!.*(?:\.\.|%2[fF]|\s)).{10,126}/$")
SUB_ID_RE=re.compile(r"^[A-Za-z0-9_-]{16,80}$")
def validate_path(value):
    if not PATH_RE.fullmatch(value): raise ValueError(f"invalid managed path: {value}")
def normalize_path(value):
    value=str(value or "")
    if not value.startswith("/"): value="/"+value
    if not value.endswith("/"): value+="/"
    validate_path(value)
    return value
def client_subscription_paths(config):
    base=normalize_path(config.get("network",{}).get("subscription",{}).get("path",""))
    result=[]
    for client in sorted(config.get("clients",[]),key=lambda x:x.get("id","")):
        sub_id=client.get("subscription_id","")
        if not client.get("enabled",True) or not SUB_ID_RE.fullmatch(sub_id): continue
        path=base if not result else f"{base.rstrip('/')}/{sub_id}/"
        validate_path(path)
        result.append((client,sub_id,path))
    return result
def render_location(path,port,allowed_ips):
    validate_path(path); lines=[f"location {path} {{"]
    for address in allowed_ips: ipaddress.ip_address(address); lines.append(f"    allow {address};")
    if allowed_ips: lines += ["    deny all;",""]
    lines += [f"    proxy_pass http://127.0.0.1:{int(port)};","    proxy_http_version 1.1;","    proxy_set_header Host $host;","    proxy_set_header X-Forwarded-Proto https;","    proxy_set_header X-Forwarded-Host $host;","    proxy_set_header X-Forwarded-Port 443;","    proxy_redirect off;","    proxy_buffering off;","    proxy_request_buffering off;","}"]
    return "\n".join(lines)
def render(config):
    subscription_path=normalize_path(config.get("network",{}).get("subscription",{}).get("path",""))
    seen={config.get("panel",{}).get("path"),config.get("network",{}).get("xhttp",{}).get("path")}; blocks=[]
    for client,sub_id,path in client_subscription_paths(config):
        if path in seen: raise ValueError(f"managed path collision: {path}")
        seen.add(path); blocks.append("\n".join([f"location = {path} {{","    root /var/www/wavemesh-sub/users;",f"    try_files /{sub_id}.txt =404;","    default_type text/plain;",'    add_header Cache-Control "no-store" always;','    add_header Profile-Title "base64:V2F2ZU1lc2hWUE4=" always;',"}"]))
    if not any(path == subscription_path for _,_,path in client_subscription_paths(config)):
        seen.add(subscription_path)
    for peer in sorted(config.get("relay_peers",[]),key=lambda x:x["id"]):
        if not peer.get("enabled",True): continue
        inbound=peer["inbound"]; path=inbound["public_path"]
        if path in seen: raise ValueError(f"managed path collision: {path}")
        seen.add(path); blocks.append(render_location(path,inbound["local_port"],peer.get("allowed_entry_ips",[])))
    for route in sorted(config.get("routes",[]),key=lambda x:(x.get("sort_order",0),x["id"])):
        if not route.get("enabled",True): continue
        kind=route.get("kind")
        if kind=="cascade": publish=True
        elif kind=="auto": publish=route.get("presentation",{}).get("published",False)
        else: publish=False
        if not publish: continue
        entry=route["entry"]; path=entry["public_path"]
        if path in seen: raise ValueError(f"managed path collision: {path}")
        seen.add(path); blocks.append(render_location(path,entry["local_port"],[]))
    return "\n\n".join(blocks)+(chr(10) if blocks else "")
def main():
    p=argparse.ArgumentParser(); p.add_argument("--config",required=True); p.add_argument("--output",required=True); a=p.parse_args()
    Path(a.output).write_text(render(json.loads(Path(a.config).read_text(encoding="utf-8"))),encoding="utf-8")
if __name__=="__main__": main()
