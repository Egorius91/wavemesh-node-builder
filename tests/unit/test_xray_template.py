import json, subprocess, sys, tempfile
from pathlib import Path

root=Path(__file__).resolve().parents[2]
tool=root/"scripts/lib/xray_template.py"
template=root/"tests/fixtures/xray-template.json"
outbound=root/"tests/fixtures/xray-outbound-de.json"
with tempfile.TemporaryDirectory() as name:
    first=Path(name)/"first.json"; second=Path(name)/"second.json"; removed=Path(name)/"removed.json"
    def merge(source, output):
        subprocess.run([sys.executable,str(tool),"merge","--template",str(source),"--outbound",str(outbound),"--inbound-tag","wm-route-de-fra-1","--outbound-tag","wm-exit-de-fra-1","--rule-tag","wm-rule-de-fra-1","--output",str(output)],check=True)
    merge(template, first)
    merge(first, second)
    data=json.loads(second.read_text())
    assert data["api"]["tag"]=="api" and "RoutingService" in data["api"]["services"]
    assert data["inbounds"][0]["listen"]=="127.0.0.1" and data["inbounds"][0]["port"]==62789
    assert [x["tag"] for x in data["outbounds"]].count("wm-exit-de-fra-1")==1
    assert data["outbounds"][0]["tag"]=="user-proxy"
    tags=[x.get("ruleTag") for x in data["routing"]["rules"]]
    assert tags==["wm-api-rule","user-rule","wm-rule-de-fra-1","catch-all"]
    subprocess.run([sys.executable,str(tool),"remove","--template",str(second),"--inbound-tag","wm-route-de-fra-1","--outbound-tag","wm-exit-de-fra-1","--rule-tag","wm-rule-de-fra-1","--output",str(removed)],check=True)
    clean=json.loads(removed.read_text())
    assert all(x.get("tag")!="wm-exit-de-fra-1" for x in clean["outbounds"])
    assert [x.get("ruleTag") for x in clean["routing"]["rules"]]==["wm-api-rule","user-rule","catch-all"]
print("xray template tests: OK")
