"""Generate synthetic panel payloads through the real Builder rendering path."""
import copy
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/lib'))
import prepared_channels as channels
import runtime_state
from tests.unit.test_prepared_channels import fixture


def builder_loopback_cases():
    config,plan,template=fixture()
    config,_,_,_=channels.stage(config,{},'route-auto-europe',plan,template,now=100)
    route=next(r for r in config['routes'] if r['id']=='route-auto-europe')
    private=runtime_state.inbound_payload(config,route)
    assert private['settings']['clients']==[]
    # Same existing renderer with synthetic legacy managed clients exercises
    # actual panel client normalization, without admitting a prepared profile.
    legacy=copy.deepcopy(config)
    route=next(r for r in legacy['routes'] if r['id']=='route-auto-europe')
    route['kind']='cascade'
    route.pop('presentation',None)
    legacy['clients']=[{'id':'synthetic-'+str(i),'name':'Fixture','enabled':enabled,'subscription_id':'synthetic-sub-'+str(i),
                       'credentials':[{'route_id':route['id'],'uuid':f'00000000-0000-4000-8000-{i:012d}','email':'fixture-'+str(i),'enabled':enabled}]} for i,enabled in ((1,True),(2,False))]
    populated=runtime_state.inbound_payload(legacy,route)
    assert len(populated['settings']['clients'])==2
    return {'schema_version':1,'generator':'runtime_state.inbound_payload after prepared_channels.stage',
            'cases':[{'name':'prepared-unpublished-empty','payload':private},
                     {'name':'managed-dynamic-clients','payload':populated}]}
