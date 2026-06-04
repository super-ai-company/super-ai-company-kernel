#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, subprocess, sys
from pathlib import Path

def openclaw_root() -> Path:
    env = os.environ.get('OPENCLAW_ROOT')
    if env:
        return Path(env).expanduser()
    if Path('/Users/shift/openclaw').exists():
        return Path('/Users/shift/openclaw')
    return Path.home() / 'openclaw'


ROOT = openclaw_root()
WORKSPACE = Path(os.environ.get('OPENCLAW_WORKSPACE', ROOT / 'workspace-main')).expanduser()
if 'OPENCLAW_WORKSPACE' not in os.environ and Path('/Users/shift/openclaw/workspace-xmanx').exists():
    WORKSPACE = Path('/Users/shift/openclaw/workspace-xmanx')
REG=Path(os.environ.get('OPENCLAW_AGENT_REGISTRY', WORKSPACE / 'config' / 'agent_registry.json')).expanduser()

def load():
    if not REG.exists():
        return {'agents': {}}
    return json.loads(REG.read_text())
def norm(s): return ''.join(str(s).lower().replace('-', '').replace('_','').split())
def resolve(q):
    data=load(); nq=norm(q); hits=[]
    for aid,info in data['agents'].items():
        vals=[aid, info.get('role','')] + info.get('aliases',[])
        for v in vals:
            nv=norm(v)
            if nq==nv or (nq and nq in nv):
                hits.append({'agent_id':aid,'matched':v,'workspace':info.get('workspace'),'role':info.get('role')}); break
    return hits
def discover():
    data=load(); known=set(data['agents'])
    cp=subprocess.run(['openclaw','agents','list','--json'],text=True,capture_output=True)
    out={'exit_code':cp.returncode,'stderr':cp.stderr,'unknown_agents':[]}
    if cp.returncode==0:
        arr=json.loads(cp.stdout)
        for a in arr:
            if a.get('id') not in known:
                out['unknown_agents'].append({'id':a.get('id'),'name':a.get('name'),'workspace':a.get('workspace'),'identityName':a.get('identityName')})
    return out
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('query', nargs='?')
    ap.add_argument('--discover', action='store_true')
    args=ap.parse_args()
    if args.discover: print(json.dumps(discover(),ensure_ascii=False,indent=2)); return
    print(json.dumps({'query':args.query,'matches':resolve(args.query or '')},ensure_ascii=False,indent=2))
if __name__=='__main__': main()
