#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, secrets, sys
from datetime import datetime
from pathlib import Path
from agent_comm_contract import normalize_optional, request_requires_strict_fields, require_execution_fields

def openclaw_root() -> Path:
    env = os.environ.get('OPENCLAW_ROOT')
    if env:
        return Path(env).expanduser()
    if Path('/Users/owner/openclaw').exists():
        return Path('/Users/owner/openclaw')
    return Path.home() / 'openclaw'


ROOT = openclaw_root()
MAIN_WS=Path(os.environ.get('OPENCLAW_WORKSPACE', ROOT / 'workspace-main')).expanduser()
if 'OPENCLAW_WORKSPACE' not in os.environ and Path('/Users/owner/openclaw/workspace-xmanx').exists():
    MAIN_WS=Path('/Users/owner/openclaw/workspace-xmanx')
REG=MAIN_WS/'config'/'agent_registry.json'
BUS=Path(os.environ.get('OPENCLAW_AGENT_BUS', ROOT / 'ops' / 'agent_bus')).expanduser()
REQUEST_TYPES={'blocker','bug_report','debug_request','approval_request','ops_request','review_request','governance_request','cross_agent_handoff','evidence_for_verification'}
PRIORITY={'P1','P2','P3'}
COMPANY_EMPLOYEE_ALIASES={
  'codex': ['codex', 'engineering', 'engineer'],
  'hermes': ['hermes', 'supervisor'],
  'claude': ['claude'],
  'trae': ['trae'],
  'antigravity': ['antigravity', 'ag'],
  'openclaw-main': ['openclaw-main', 'openclaw main', 'company-main'],
}

def reg_agents():
    if not REG.exists():
        return {}
    return json.loads(REG.read_text()).get('agents',{})

def norm(s): return ''.join(str(s).lower().replace('-', '').replace('_','').split())

def resolve_agent(q):
    agents=reg_agents(); nq=norm(q)
    for aid,info in agents.items():
        vals=[aid]+info.get('aliases',[])
        if any(nq==norm(v) for v in vals): return aid
    if q in agents: return q
    for aid,aliases in COMPANY_EMPLOYEE_ALIASES.items():
        vals=[aid]+aliases
        if any(nq==norm(v) for v in vals): return aid
    raise SystemExit(f'unknown agent in registry: {q}')

def now(): return datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
def build_id(): return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"

def write_json(p,obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n')

def main():
    ap=argparse.ArgumentParser(description='Submit a structured request from an agent to main via registry-driven agent_bus. Default is dry-run.')
    ap.add_argument('--agent', required=True)
    ap.add_argument('--request-type', required=True, choices=sorted(REQUEST_TYPES))
    ap.add_argument('--priority', default='P2', choices=sorted(PRIORITY))
    ap.add_argument('--objective', required=True)
    ap.add_argument('--context', default='')
    ap.add_argument('--evidence-path', action='append', default=[])
    ap.add_argument('--requested-action', required=True)
    ap.add_argument('--next-command', default='', help='explicit next command main should preserve in final receipt')
    ap.add_argument('--expected-completion-evidence', default='', help='explicit evidence expected when the task is complete')
    ap.add_argument('--business-impact', default='not specified')
    ap.add_argument('--urgency', default='normal')
    ap.add_argument('--rollback', default='No runtime change requested by this submission; main may reject or ask for clarification.')
    ap.add_argument('--apply', action='store_true', help='actually submit to main inbox')
    args=ap.parse_args()
    if request_requires_strict_fields(args.request_type, args.priority):
        require_execution_fields(
            {
                'next_command': args.next_command,
                'expected_completion_evidence': args.expected_completion_evidence,
            },
            fields=('next_command', 'expected_completion_evidence'),
            error_prefix='request_main_required_fields',
        )
    agent=resolve_agent(args.agent)
    payload={
      'requesting_agent': agent,
      'request_type': args.request_type,
      'priority': args.priority,
      'objective': args.objective,
      'context': args.context,
      'evidence_paths': args.evidence_path,
      'requested_action_from_main': args.requested_action,
      'next_command': normalize_optional(args.next_command),
      'expected_completion_evidence': normalize_optional(args.expected_completion_evidence),
      'business_impact': args.business_impact,
      'deadline_or_urgency': args.urgency,
      'rollback_or_safety_note': args.rollback,
      'receipt_required_from_main': True,
      'created_at': now(),
    }
    task_type=f'agent_request_{args.request_type}'
    task_id=build_id()
    obj={'task_id':task_id,'created_at':now(),'source_agent':agent,'target_agent':'main','type':task_type,'priority':args.priority,'payload':payload,'rollback':args.rollback,'status':'submitted'}
    out=BUS/'inbox'/'main'/f'{task_id}.json'
    if not args.apply:
        print(json.dumps({'dry_run':True,'meaning':'not submitted; rerun with --apply to write main inbox','target_file':str(out),'task':obj},ensure_ascii=False,indent=2)); return
    write_json(out,obj)
    print(json.dumps({'dry_run':False,'ok':True,'task_id':task_id,'file':str(out),'payload':payload},ensure_ascii=False,indent=2))
if __name__=='__main__': main()
