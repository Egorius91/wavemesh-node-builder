#!/usr/bin/env python3
"""Build and compare WaveMesh-managed 3X-UI XHTTP inbounds."""

import argparse, json
from pathlib import Path


def as_object(value):
    if isinstance(value, str):
        try: return json.loads(value)
        except json.JSONDecodeError: return {}
    return value if isinstance(value, dict) else {}


def build(args):
    clients=json.loads(Path(args.clients).read_text(encoding="utf-8"))
    public_name=args.remark or args.tag
    payload={
        "up":0,"down":0,"total":0,"remark":public_name,"tag":args.tag,"enable":True,"expiryTime":0,
        "listen":"127.0.0.1","port":args.port,"protocol":"vless",
        "settings":{"clients":clients,"decryption":"none","fallbacks":[]},
        "streamSettings":{"network":"xhttp","security":"none","xhttpSettings":{"path":args.path,"host":args.host,"mode":"stream-one"}},
        "sniffing":{"enabled":True,"destOverride":["http","tls","quic","fakedns"],"metadataOnly":False,"routeOnly":False},
        "allocate":{"strategy":"always"},
    }
    if args.public_domain:
        payload["streamSettings"]["externalProxy"]=[{"dest":args.public_domain,"port":443,"remark":public_name,"forceTls":"tls","sni":args.public_domain,"fingerprint":args.fingerprint}]
    return payload


def normalized(inbound):
    stream=as_object(inbound.get("streamSettings")); settings=as_object(inbound.get("settings")); xhttp=as_object(stream.get("xhttpSettings"))
    clients=settings.get("clients") if isinstance(settings.get("clients"),list) else []
    proxies=stream.get("externalProxy") if isinstance(stream.get("externalProxy"),list) else []
    proxy_remark=next((x.get("remark") for x in proxies if isinstance(x,dict)),None)
    return {"tag":inbound.get("tag") or inbound.get("remark"),"remark":inbound.get("remark"),"external_proxy_remark":proxy_remark,"listen":inbound.get("listen"),"port":int(inbound.get("port",-1)),"protocol":inbound.get("protocol"),"path":xhttp.get("path"),"host":xhttp.get("host"),"mode":xhttp.get("mode"),"clients":sorted((x.get("id"),x.get("email"),x.get("enable",True)) for x in clients if isinstance(x,dict))}


def matching_inbound(desired, response):
    items=response.get("obj") if isinstance(response,dict) else None
    if not isinstance(items,list): raise ValueError("3X-UI inbound list response has no obj array")
    wanted=normalized(desired)
    matches=[item for item in items if (item.get("tag") or item.get("remark"))==wanted["tag"]]
    if len(matches)>1: raise ValueError(f"multiple inbounds use managed tag {wanted['tag']}")
    return matches[0] if matches else None


def client_identity(client):
    return {value for key in ("id","email","subId") if (value:=client.get(key)) not in (None,"")}


def merge_external_clients(desired, response):
    """Keep non-WaveMesh clients which another control plane added to our inbound."""
    actual=matching_inbound(desired,response)
    if actual is None: return desired
    desired_settings=as_object(desired.get("settings"))
    actual_settings=as_object(actual.get("settings"))
    managed=desired_settings.get("clients") if isinstance(desired_settings.get("clients"),list) else []
    existing=actual_settings.get("clients") if isinstance(actual_settings.get("clients"),list) else []
    identities=set().union(*(client_identity(client) for client in managed if isinstance(client,dict)))
    external=[]
    for client in existing:
        if not isinstance(client,dict): continue
        email=str(client.get("email") or "")
        identity=client_identity(client)
        if email.startswith("wm.") or identities.intersection(identity): continue
        external.append(client)
        identities.update(identity)
    desired_settings["clients"]=[client for client in managed if isinstance(client,dict)]+external
    desired["settings"]=desired_settings
    return desired


def plan(desired, response):
    wanted=normalized(desired)
    actual=matching_inbound(desired,response)
    if actual is None: return {"action":"add","id":None}
    inbound_id=actual.get("id") or actual.get("Id")
    return {"action":"noop" if normalized(actual)==wanted else "update","id":inbound_id}


def main():
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="command",required=True)
    make=sub.add_parser("build")
    make.add_argument("--tag",required=True); make.add_argument("--remark",default=""); make.add_argument("--port",type=int,required=True); make.add_argument("--path",required=True); make.add_argument("--host",required=True); make.add_argument("--clients",required=True); make.add_argument("--public-domain",default=""); make.add_argument("--fingerprint",default="chrome"); make.add_argument("--output",required=True)
    compare=sub.add_parser("plan"); compare.add_argument("--desired",required=True); compare.add_argument("--actual",required=True)
    merge=sub.add_parser("merge-clients"); merge.add_argument("--desired",required=True); merge.add_argument("--actual",required=True); merge.add_argument("--output",required=True)
    args=parser.parse_args()
    if args.command=="build":
        Path(args.output).write_text(json.dumps(build(args),indent=2,sort_keys=True)+"\n",encoding="utf-8")
    elif args.command=="plan":
        print(json.dumps(plan(json.loads(Path(args.desired).read_text(encoding="utf-8")),json.loads(Path(args.actual).read_text(encoding="utf-8"))),separators=(",",":")))
    else:
        desired=json.loads(Path(args.desired).read_text(encoding="utf-8")); actual=json.loads(Path(args.actual).read_text(encoding="utf-8"))
        Path(args.output).write_text(json.dumps(merge_external_clients(desired,actual),indent=2,sort_keys=True)+"\n",encoding="utf-8")


if __name__=="__main__": main()
