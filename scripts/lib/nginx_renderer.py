#!/usr/bin/env python3
import argparse, ipaddress, json, re
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from route_presentation import route_is_public

PATH_RE=re.compile(r"^/(?!.*(?:\.\.|%2[fF]|\s)).{10,126}/$")
SUB_ID_RE=re.compile(r"^[A-Za-z0-9_-]{16,80}$")
STREAM_TIMEOUT_DIRECTIVES=[
    "    proxy_connect_timeout 10s;",
    "    proxy_send_timeout 300s;",
    "    proxy_read_timeout 300s;",
    "    send_timeout 300s;",
]
def validate_path(value):
    if not PATH_RE.fullmatch(value): raise ValueError(f"invalid managed path: {value}")
def normalize_path(value):
    value=str(value or "")
    if not value.startswith("/"): value="/"+value
    if not value.endswith("/"): value+="/"
    validate_path(value)
    return value
def configured_subscription_base(config):
    value=config.get("network",{}).get("subscription",{}).get("path","")
    if not value:
        return None
    value=normalize_path(value)
    if value.startswith("/sub/"):
        return None
    return value
def subscription_backend(config):
    item=config.get("network",{}).get("subscription",{})
    return item.get("backend") or item.get("mode") or "generated"
def client_subscription_paths(config):
    base=configured_subscription_base(config)
    result=[]
    for client in sorted(config.get("clients",[]),key=lambda x:x.get("id","")):
        sub_id=client.get("subscription_id","")
        if not client.get("enabled",True) or not SUB_ID_RE.fullmatch(sub_id): continue
        if base:
            path=base if not result else f"{base.rstrip('/')}/{sub_id}/"
        else:
            path=f"/sub/{sub_id}/"
        validate_path(path)
        result.append((client,sub_id,path))
    return result
def render_location(path,port,allowed_ips,stream_timeouts=False):
    validate_path(path); lines=[f"location {path} {{"]
    for address in allowed_ips: ipaddress.ip_address(address); lines.append(f"    allow {address};")
    if allowed_ips: lines += ["    deny all;",""]
    lines += [f"    proxy_pass http://127.0.0.1:{int(port)};","    proxy_http_version 1.1;","    proxy_set_header Host $host;","    proxy_set_header X-Forwarded-Proto https;","    proxy_set_header X-Forwarded-Host $host;","    proxy_set_header X-Forwarded-Port 443;","    proxy_redirect off;"]
    if stream_timeouts: lines += STREAM_TIMEOUT_DIRECTIVES
    lines += ["    proxy_buffering off;","    proxy_request_buffering off;","}"]
    return "\n".join(lines)
def render_native_alias(path,target,port):
    validate_path(path); target=normalize_path(target)
    return "\n".join([f"location {path} {{",f"    proxy_pass http://127.0.0.1:{int(port)}{target};","    proxy_http_version 1.1;","    proxy_set_header Host $host;","    proxy_set_header X-Forwarded-Proto https;","    proxy_set_header X-Forwarded-Host $host;","    proxy_set_header X-Forwarded-Port 443;","    proxy_redirect off;","    proxy_buffering off;","    proxy_request_buffering off;","}"])
def render(config,additional_native_path=None,native_alias=None,additional_native_port=None):
    subscription_path=configured_subscription_base(config)
    seen={config.get("panel",{}).get("path"),config.get("network",{}).get("xhttp",{}).get("path")}; blocks=[]
    subscription_paths=[]
    if subscription_backend(config)=="xui-native":
        if not subscription_path: raise ValueError("xui-native backend requires an opaque subscription path")
        if subscription_path in seen: raise ValueError(f"managed path collision: {subscription_path}")
        seen.add(subscription_path)
        blocks.append(render_location(subscription_path,config.get("network",{}).get("subscription",{}).get("local_port",2096),[]))
    else:
        subscription_paths=client_subscription_paths(config)
        for client,sub_id,path in subscription_paths:
            if path in seen: raise ValueError(f"managed path collision: {path}")
            seen.add(path); blocks.append("\n".join([f"location = {path} {{","    root /var/www/wavemesh-sub/users;",f"    try_files /{sub_id}.txt =404;","    default_type text/plain;",'    add_header Cache-Control "no-store" always;','    add_header Profile-Title "base64:V2F2ZU1lc2hWUE4=" always;',"}"]))
        if subscription_path and not any(path == subscription_path for _,_,path in subscription_paths):
            seen.add(subscription_path)
    native_port=config.get("network",{}).get("subscription",{}).get("local_port",2096)
    if additional_native_path:
        additional_native_path=normalize_path(additional_native_path)
        if additional_native_path in seen: raise ValueError(f"managed path collision: {additional_native_path}")
        port=native_port if additional_native_port is None else int(additional_native_port)
        seen.add(additional_native_path); blocks.append(render_location(additional_native_path,port,[]))
    if native_alias:
        alias_from,alias_to=native_alias
        alias_from=normalize_path(alias_from); alias_to=normalize_path(alias_to)
        if alias_from in seen: raise ValueError(f"managed path collision: {alias_from}")
        seen.add(alias_from); blocks.append(render_native_alias(alias_from,alias_to,native_port))
    for peer in sorted(config.get("relay_peers",[]),key=lambda x:x["id"]):
        if not peer.get("enabled",True): continue
        inbound=peer["inbound"]; path=inbound["public_path"]
        if path in seen: raise ValueError(f"managed path collision: {path}")
        seen.add(path); blocks.append(render_location(path,inbound["local_port"],peer.get("allowed_entry_ips",[]),stream_timeouts=True))
    for route in sorted(config.get("routes",[]),key=lambda x:(x.get("sort_order",0),x["id"])):
        if not route.get("enabled",True): continue
        kind=route.get("kind")
        if kind not in ("cascade","auto"): continue
        if not route_is_public(config,route,manual_default=kind=="cascade"): continue
        entry=route["entry"]; path=entry["public_path"]
        if path in seen: raise ValueError(f"managed path collision: {path}")
        seen.add(path); blocks.append(render_location(path,entry["local_port"],[],stream_timeouts=True))
    return "\n\n".join(blocks)+(chr(10) if blocks else "")
def main():
    p=argparse.ArgumentParser(); p.add_argument("--config",required=True); p.add_argument("--output",required=True); p.add_argument("--additional-native-path"); p.add_argument("--additional-native-port",type=int); p.add_argument("--native-alias-from"); p.add_argument("--native-alias-to"); a=p.parse_args()
    if a.additional_native_port is not None and not a.additional_native_path: p.error("--additional-native-port requires --additional-native-path")
    if bool(a.native_alias_from) != bool(a.native_alias_to): p.error("--native-alias-from and --native-alias-to must be used together")
    alias=(a.native_alias_from,a.native_alias_to) if a.native_alias_from else None
    Path(a.output).write_text(render(json.loads(Path(a.config).read_text(encoding="utf-8")),a.additional_native_path,alias,a.additional_native_port),encoding="utf-8")
if __name__=="__main__": main()
