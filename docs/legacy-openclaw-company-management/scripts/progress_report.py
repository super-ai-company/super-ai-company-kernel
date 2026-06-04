#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
from datetime import datetime
from pathlib import Path


def openclaw_root() -> Path:
    env = os.environ.get('OPENCLAW_ROOT')
    if env:
        return Path(env).expanduser()
    if Path('/Users/shift/openclaw').exists():
        return Path('/Users/shift/openclaw')
    return Path.home() / 'openclaw'

ROOT = openclaw_root()
BUS = Path(os.environ.get('OPENCLAW_AGENT_BUS', ROOT / 'ops' / 'agent_bus')).expanduser()
STATES = {'acknowledged', 'in_progress', 'blocked', 'completed'}


def now():
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def build_id():
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def write_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def main():
    ap = argparse.ArgumentParser(description='Send a structured progress report from Codex/Agent to Main.')
    ap.add_argument('--agent', default='codex', help='The reporting agent (default: codex)')
    ap.add_argument('--state', required=True, choices=sorted(STATES), help='The exact progress state')
    ap.add_argument('--project', required=True, help='Project name or repository path')
    
    # Payload details
    ap.add_argument('--targets', help='Files to be changed (for acknowledged/in_progress)')
    ap.add_argument('--action', help='What is being modified or was changed')
    ap.add_argument('--checking', help='What is being checked/tested (for in_progress/completed)')
    ap.add_argument('--risks', help='Are there any risks / Remaining risks')
    ap.add_argument('--blocked-on', help='Where is it stuck (for blocked)')
    ap.add_argument('--tried', help='What has been tried (for blocked)')
    ap.add_argument('--needs-action-from', help='Who needs to do what (for blocked)')
    
    ap.add_argument('--apply', action='store_true', help='Actually submit to main inbox')
    args = ap.parse_args()

    payload = {
        'state': args.state,
        'project': args.project,
        'targets': args.targets or '',
        'action': args.action or '',
        'checking': args.checking or '',
        'risks': args.risks or '',
        'blocked_on': args.blocked_on or '',
        'tried': args.tried or '',
        'needs_action_from': args.needs_action_from or ''
    }

    # Validate state-specific required fields
    if args.state == 'acknowledged':
        if not args.targets: ap.error("--targets (准备改哪些文件) is required for 'acknowledged'")
    elif args.state == 'in_progress':
        if not args.action: ap.error("--action (正在改什么) is required for 'in_progress'")
        if not args.checking: ap.error("--checking (当前检查什么) is required for 'in_progress'")
    elif args.state == 'blocked':
        if not args.blocked_on: ap.error("--blocked-on (卡在哪) is required for 'blocked'")
        if not args.tried: ap.error("--tried (试了什么) is required for 'blocked'")
        if not args.needs_action_from: ap.error("--needs-action-from (需要谁做什么) is required for 'blocked'")
    elif args.state == 'completed':
        if not args.action: ap.error("--action (改了什么) is required for 'completed'")
        if not args.checking: ap.error("--checking (验证了什么) is required for 'completed'")

    task_id = build_id()
    obj = {
        'task_id': task_id,
        'created_at': now(),
        'source_agent': args.agent,
        'target_agent': 'main',
        'type': 'progress_update',
        'status': args.state,
        'payload': payload
    }

    out = BUS / 'inbox' / 'main' / f'progress_{args.state}_{task_id}.json'

    if not args.apply:
        print(json.dumps({'dry_run': True, 'target_file': str(out), 'report': obj}, ensure_ascii=False, indent=2))
        return

    write_json(out, obj)
    print(json.dumps({'dry_run': False, 'ok': True, 'task_id': task_id, 'file': str(out), 'report': payload}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
