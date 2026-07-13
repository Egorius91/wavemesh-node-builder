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
        "up":0,"down":0,"total":0,"remark":args.tag,"tag":args.tag,"enable":True,"expiryTime":0,
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


def plan(desired, response):
    items=response.get("obj") if isinstance(response,dict) else None
    if not isinstance(items,list): raise ValueError("3X-UI inbound list response has no obj array")
    wanted=normalized(desired)
    matches=[item for item in items if (item.get("tag") or item.get("remark"))==wanted["tag"]]
    if len(matches)>1: raise ValueError(f"multiple inbounds use managed tag {wanted['tag']}")
    if not matches: return {"action":"add","id":None}
    inbound_id=matches[0].get("id") or matches[0].get("Id")
    return {"action":"noop" if normalized(matches[0])==wanted else "update","id":inbound_id}


def main():
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="command",required=True)
    make=sub.add_parser("build")
    make.add_argument("--tag",required=True); make.add_argument("--remark",default=""); make.add_argument("--port",type=int,required=True); make.add_argument("--path",required=True); make.add_argument("--host",required=True); make.add_argument("--clients",required=True); make.add_argument("--public-domain",default=""); make.add_argument("--fingerprint",default="chrome"); make.add_argument("--output",required=True)
    compare=sub.add_parser("plan"); compare.add_argument("--desired",required=True); compare.add_argument("--actual",required=True)
    args=parser.parse_args()
    if args.command=="build":
        Path(args.output).write_text(json.dumps(build(args),indent=2,sort_keys=True)+"\n",encoding="utf-8")
    else:
        print(json.dumps(plan(json.loads(Path(args.desired).read_text(encoding="utf-8")),json.loads(Path(args.actual).read_text(encoding="utf-8"))),separators=(",",":")))


if __name__=="__main__": main()
