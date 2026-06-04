#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, shutil, hashlib
from datetime import datetime
from pathlib import Path
import subprocess

def openclaw_root() -> Path:
    env = os.environ.get('OPENCLAW_ROOT')
    if env:
        return Path(env).expanduser()
    if Path('/Users/owner/openclaw').exists():
        return Path('/Users/owner/openclaw')
    return Path.home() / 'openclaw'


ROOT = openclaw_root()
MAIN_WORKSPACE = Path(os.environ.get('OPENCLAW_WORKSPACE', ROOT / 'workspace-main')).expanduser()
if 'OPENCLAW_WORKSPACE' not in os.environ and Path('/Users/owner/openclaw/workspace-xmanx').exists():
    MAIN_WORKSPACE = Path('/Users/owner/openclaw/workspace-xmanx')
BUS=Path(os.environ.get('OPENCLAW_AGENT_BUS', ROOT / 'ops' / 'agent_bus')).expanduser()
WORKSPACES={
 'main': MAIN_WORKSPACE,
 'nestcar': ROOT / 'workspace-nestcar',
 'chindahotpot': ROOT / 'workspace-chindahotpot',
 'invest': ROOT / 'workspace-invest',
 'video-creator': ROOT / 'workspace-video-creator',
 'video-publisher': ROOT / 'workspace-video-publisher',
 'video-ops': ROOT / 'workspace-video-ops',
}

def sha256_file(p: Path) -> str:
    h=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()

def load_task(path: Path):
    return json.loads(path.read_text())

def receipt_for(agent: str, task_path: Path, status: str, note: str):
    ws=WORKSPACES[agent]
    task=load_task(task_path)
    task_id=task.get('task_id') or task_path.stem
    report_dir=ws/'reports'/'agent-bus-receipts'
    report_dir.mkdir(parents=True, exist_ok=True)
    receipt={
      'task_id': task_id,
      'task_file': str(task_path),
      'task_sha256': sha256_file(task_path),
      'source_agent': task.get('source_agent') or task.get('source'),
      'target_agent': agent,
      'type': task.get('type'),
      'priority': task.get('priority'),
      'status': status,
      'what_changed': note,
      'evidence_path': None,
      'memory_updated': 'not_requested',
      'blocker': None if status in ('acknowledged','applied','skipped') else note,
      'next_action': 'main_verify_receipt' if status != 'blocked' else 'main_resolve_blocker',
      'timestamp': datetime.now().isoformat(timespec='seconds'),
    }
    payload=task.get('payload')
    if isinstance(payload, dict):
        summary=payload.get('summary') or payload.get('title')
        if summary:
            receipt['task_summary']=summary
    receipt_path=report_dir/f'{task_id}.receipt.json'
    receipt['evidence_path']=str(receipt_path)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    done_dir=BUS/'done'/agent
    try:
        done_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(receipt_path, done_dir/f'{task_id}.receipt.json')
    except Exception as e:
        receipt['done_copy_error']=str(e)
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    return receipt

def main():
    ap=argparse.ArgumentParser(description='Process or ACK agent_bus inbox tasks.')
    ap.add_argument('--agent', required=True, choices=sorted(WORKSPACES))
    ap.add_argument('--mode', choices=['list','ack','skip','process'], default='list')
    ap.add_argument('--limit', type=int, default=10)
    ap.add_argument('--type', default=None, help='optional task type filter')
    args=ap.parse_args()
    inbox=BUS/'inbox'/args.agent
    tasks=sorted(inbox.glob('*.json'), reverse=True)[:args.limit] if inbox.exists() else []
    if args.type:
        filt=[]
        for p in tasks:
            try:
                if load_task(p).get('type') == args.type:
                    filt.append(p)
            except Exception:
                pass
        tasks=filt
    if args.mode=='list':
        print(json.dumps({'agent':args.agent,'meaning':'listed inbox tasks are persisted only, not acknowledged','tasks':[str(p) for p in tasks]}, ensure_ascii=False, indent=2)); return
    if args.mode=='process':
        results=[]
        for p in tasks:
            t=load_task(p)
            payload=t.get('payload',{})
            cmd=None
            if isinstance(payload,dict):
                cmd=payload.get('next_command') or payload.get('command')
            if not cmd:
                results.append({'file':str(p),'error':'No next_command found'})
                continue
            cmd = cmd.replace('TASK_ID', t.get('task_id') or p.stem)
            cp=subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=str(WORKSPACES[args.agent]))
            status = 'applied' if cp.returncode==0 else 'blocked'
            note = f"exit_code={cp.returncode} stdout={cp.stdout[:100]} stderr={cp.stderr[:100]}"
            results.append({'file':str(p),'command':cmd,'exit_code':cp.returncode,'stdout':cp.stdout[:500],'stderr':cp.stderr[:500],'receipt':receipt_for(args.agent,p,status,note)})
        print(json.dumps({'agent':args.agent,'mode':'process','results':results}, ensure_ascii=False, indent=2)); return
    receipts=[]
    for p in tasks:
        status='acknowledged' if args.mode=='ack' else 'skipped'
        note='acknowledged inbox task; no business action executed by this helper' if args.mode=='ack' else 'skipped by operator/agent using helper; no business action executed'
        receipts.append(receipt_for(args.agent,p,status,note))
    print(json.dumps({'agent':args.agent,'mode':args.mode,'receipts':receipts}, ensure_ascii=False, indent=2))
if __name__=='__main__': main()
