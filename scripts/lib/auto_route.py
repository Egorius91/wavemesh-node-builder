#!/usr/bin/env python3
"""Build and mutate persistent WaveMesh Auto Route desired state."""
import argparse, copy, json, os, tempfile, uuid
from pathlib import Path

def load(path): return json.loads(Path(path).read_text(encoding="utf-8"))
def atomic(path,data):
    target=Path(path); target.parent.mkdir(parents=True,exist_ok=True); fd,tmp=tempfile.mkstemp(prefix=f".{target.name}.",dir=target.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as h: json.dump(data,h,indent=2,ensure_ascii=False,sort_keys=True); h.write("\n"); h.flush(); os.fsync(h.fileno())
        os.chmod(tmp,0o600); os.replace(tmp,target)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
def validate_id(value):
    if not value or len(value)>32 or not value.replace("-","").isalnum() or value.lower()!=value: raise ValueError("Auto Route id must contain lowercase letters, digits, and hyphens")
def enabled_exit_tags(config,requested):
    exits={i["id"]:i for i in config.get("exits",[]) if i.get("enabled",True)}; chosen=requested or [i["id"] for i in config.get("exits",[]) if i.get("enabled",True)]
    missing=[v for v in chosen if v not in exits]
    if missing: raise ValueError(f"unknown or disabled Exit ids: {', '.join(missing)}")
    selectors=[]
    for exit_id in chosen:
        tag=exits[exit_id].get("xray",{}).get("outbound_tag")
        if not tag or not tag.startswith("wm-exit-"): raise ValueError(f"Exit has no managed outbound tag: {exit_id}")
        selectors.append(tag)
    selectors=list(dict.fromkeys(selectors))
    if not selectors: raise ValueError("Auto Route requires at least one enabled Exit")
    return selectors
def find_auto(config,auto_id):
    route=next((i for i in config.get("routes",[]) if i.get("id")==f"route-auto-{auto_id}" and i.get("kind")=="auto"),None); balancer=next((i for i in config.get("balancers",[]) if i.get("id")==auto_id),None)
    if not route or not balancer: raise ValueError(f"Auto Route not found: {auto_id}")
    return route,balancer
def prepare(config,auto_id,display_name,requested_exits,local_port,public_path,sort_order):
    if config.get("node",{}).get("role")!="entry": raise ValueError("Auto Route requires node.role=entry")
    validate_id(auto_id)
    if any(r.get("id")==f"route-auto-{auto_id}" for r in config.get("routes",[])): raise ValueError(f"Auto Route already exists: {auto_id}")
    if not 21000<=int(local_port)<=21999: raise ValueError("Auto Route local port must be in 21000..21999")
    result=copy.deepcopy(config); selectors=enabled_exit_tags(result,requested_exits); route_id=f"route-auto-{auto_id}"; inbound_tag=f"wm-route-auto-{auto_id}"; balancer_tag=f"wm-balancer-{auto_id}"; rule_tag=f"wm-rule-auto-{auto_id}"
    used={c.get("uuid") for client in result.get("clients",[]) for c in client.get("credentials",[])}; inbound_clients=[]
    for client in result.get("clients",[]):
        value=str(uuid.uuid4())
        while value in used: value=str(uuid.uuid4())
        used.add(value); email=f"wm.{client['id']}.{route_id}"; client.setdefault("credentials",[]).append({"route_id":route_id,"email":email,"uuid":value,"enabled":True}); inbound_clients.append({"id":value,"email":email,"enable":bool(client.get("enabled",True)),"flow":"","limitIp":0,"totalGB":0,"expiryTime":0,"tgId":0,"subId":client.get("subscription_id","")})
    if not inbound_clients: raise ValueError("Entry has no clients")
    result.setdefault("balancers",[]).append({"id":auto_id,"display_name":display_name,"enabled":True,"strategy":"leastPing","selector":selectors,"balancer_tag":balancer_tag,"observatory_tag":f"wm-observatory-{auto_id}"})
    result.setdefault("routes",[]).append({"id":route_id,"kind":"auto","display_name":display_name,"enabled":True,"sort_order":int(sort_order),"balancer_id":auto_id,"entry":{"listen":"127.0.0.1","local_port":int(local_port),"public_path":public_path,"inbound_id":None,"inbound_tag":inbound_tag,"xhttp_mode":"stream-one"},"routing":{"rule_tag":rule_tag,"balancer_tag":balancer_tag},"presentation":{"published":False}})
    return result,inbound_clients
def finalize(config,route_id,inbound_id):
    result=copy.deepcopy(config); route=next((i for i in result.get("routes",[]) if i.get("id")==route_id and i.get("kind")=="auto"),None)
    if not route: raise ValueError(f"Auto Route not found: {route_id}")
    route["entry"]["inbound_id"]=int(inbound_id); return result
def set_enabled(config,auto_id,enabled):
    result=copy.deepcopy(config); route,balancer=find_auto(result,auto_id); route["enabled"]=bool(enabled); balancer["enabled"]=bool(enabled)
    for client in result.get("clients",[]):
        for credential in client.get("credentials",[]):
            if credential.get("route_id")==route["id"]: credential["enabled"]=bool(enabled)
    return result
def set_published(config,auto_id,published):
    result=copy.deepcopy(config); route,balancer=find_auto(result,auto_id)
    if published and not (route.get("enabled",True) and balancer.get("enabled",True)): raise ValueError("disabled Auto Route cannot be published")
    route.setdefault("presentation",{})["published"]=bool(published); return result
def describe(config,auto_id):
    route,balancer=find_auto(config,auto_id); return {"route_id":route["id"],"inbound_id":route["entry"]["inbound_id"],"inbound_tag":route["entry"]["inbound_tag"],"balancer_tag":route["routing"]["balancer_tag"],"rule_tag":route["routing"]["rule_tag"],"strategy":balancer["strategy"],"selectors":balancer["selector"],"enabled":bool(route.get("enabled",True) and balancer.get("enabled",True)),"published":route.get("presentation",{}).get("published",False)}
def main():
    p=argparse.ArgumentParser(); s=p.add_subparsers(dest="command",required=True)
    b=s.add_parser("prepare"); [b.add_argument(x,required=True) for x in ("--config","--candidate","--clients","--id","--display-name","--port","--path")]; b.add_argument("--exit-ids",default=""); b.add_argument("--sort-order",type=int,default=50)
    f=s.add_parser("finalize"); f.add_argument("--candidate",required=True); f.add_argument("--route-id",required=True); f.add_argument("--inbound-id",type=int,required=True)
    t=s.add_parser("set-enabled"); t.add_argument("--config",required=True); t.add_argument("--output",required=True); t.add_argument("--id",required=True); t.add_argument("--enabled",choices=("true","false"),required=True)
    q=s.add_parser("set-published"); q.add_argument("--config",required=True); q.add_argument("--output",required=True); q.add_argument("--id",required=True); q.add_argument("--published",choices=("true","false"),required=True)
    d=s.add_parser("describe"); d.add_argument("--config",required=True); d.add_argument("--id",required=True); a=p.parse_args()
    if a.command=="prepare":
        candidate,clients=prepare(load(a.config),a.id,a.display_name,[v.strip() for v in a.exit_ids.split(",") if v.strip()],int(a.port),a.path,a.sort_order); atomic(a.candidate,candidate); atomic(a.clients,clients)
    elif a.command=="finalize": atomic(a.candidate,finalize(load(a.candidate),a.route_id,a.inbound_id))
    elif a.command=="set-enabled": atomic(a.output,set_enabled(load(a.config),a.id,a.enabled=="true"))
    elif a.command=="set-published": atomic(a.output,set_published(load(a.config),a.id,a.published=="true"))
    else: print(json.dumps(describe(load(a.config),a.id),ensure_ascii=False))
if __name__=="__main__": main()
