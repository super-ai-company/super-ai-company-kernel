#!/usr/bin/env python3
from __future__ import annotations

"""
统一外发接口层 (Unified Outbound Gateway)
功能：通过一个入口发送消息到 LINE / Telegram / WeChat，自动选择可用 channel

用法：
  python3 unified_outbound.py send --platform line --target GROUP_ID --message "你好"
  python3 unified_outbound.py send --platform telegram --target CHAT_ID --message "你好"
  python3 unified_outbound.py check --platform line    # 显示LINE账号状态

依赖：
  - openclaw CLI（用于 Telegram）
  - LINE token 直接从 openclaw.json 读取
  - 当前窗口 WeChat 通过当前会话发送
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def openclaw_root() -> Path:
    env = os.environ.get('OPENCLAW_ROOT')
    if env:
        return Path(env).expanduser()
    if Path('/Users/owner/openclaw').exists():
        return Path('/Users/owner/openclaw')
    return Path.home() / 'openclaw'


ROOT = openclaw_root()
OPENCLAW_CONFIG = Path(os.environ.get('OPENCLAW_CONFIG', ROOT / 'openclaw.json')).expanduser()
WORKSPACE = Path(os.environ.get('OPENCLAW_WORKSPACE', ROOT / 'workspace-main')).expanduser()
if 'OPENCLAW_WORKSPACE' not in os.environ and Path('/Users/owner/openclaw/workspace-xmanx').exists():
    WORKSPACE = Path('/Users/owner/openclaw/workspace-xmanx')

# 统一入口 — 查 skill_accounts.db，不再硬编码业务脚本路径
SKILL_DB = WORKSPACE / 'scripts' / 'skill_accounts_db.py'
UNIFIED_BROWSER = WORKSPACE / 'scripts' / 'unified_browser.py'
# 保留 legacy 脚本路径作为 fallback
CHINDA_LINE_SCRIPT = ROOT / 'workspace-chindahotpot' / 'scripts' / 'send_validated_store_pushes.py'
NESTCAR_LINE_SCRIPT = ROOT / 'workspace-nestcar' / 'scripts' / 'send_line_push.py'


def load_config():
    return json.loads(OPENCLAW_CONFIG.read_text())


def resolve_openclaw_bin() -> str:
    env_bin = os.environ.get('OPENCLAW_BIN', '').strip()
    if env_bin:
        return env_bin
    found = shutil.which('openclaw')
    if found:
        return found
    return '/opt/homebrew/bin/openclaw'


def check_line_status() -> dict:
    """检查所有 LINE 账号的可用性"""
    config = load_config()
    accounts = config.get('channels', {}).get('line', {}).get('accounts', {})
    results = {}
    for name, acct in sorted(accounts.items()):
        token = acct.get('channelAccessToken', '')
        enabled = acct.get('enabled', False)
        results[name] = {
            'enabled': bool(enabled),
            'has_token': bool(token),
            'token_len': len(token) if token else 0,
            'bot_id': acct.get('botId') or 'not_set',
        }
    return results


def send_line_via_openclaw(target: str, message: str) -> dict:
    """通过 openclaw message send 走 LINE channel"""
    cli = resolve_openclaw_bin()
    r = subprocess.run([
        cli, 'message', 'send',
        '--channel', 'line',
        '--account', 'nestcar',
        '--target', target,
        '--message', message,
        '--json'
    ], capture_output=True, text=True, timeout=30)
    return {
        'status': 'ok' if r.returncode == 0 else 'error',
        'returncode': r.returncode,
        'stdout': r.stdout[:500],
        'stderr': r.stderr[:500],
    }


def send_line_via_script(target: str, message: str, account: str = 'nestcar') -> dict:
    """优先查数据库路由 + 统一入口发送，失败后尝试 legacy 脚本"""
    try:
        r = subprocess.run([sys.executable, str(SKILL_DB), 'get',
            '--business', account, '--platform', 'line-official-manager', '--action', 'send'],
            capture_output=True, text=True, timeout=15)
        acc = json.loads(r.stdout)
        if acc.get('found'):
            r2 = subprocess.run([sys.executable, str(UNIFIED_BROWSER), 'execute',
                '--business', account, '--platform', 'line-official-manager', '--action', 'send',
                '--target', target, '--message', message],
                capture_output=True, text=True, timeout=300)
            try:
                result = json.loads(r2.stdout)
                if result.get('ok'):
                    return {'ok': True, 'method': 'unified_browser', 'result': result}
            except json.JSONDecodeError:
                pass
    except Exception:
        pass

    # fallback: legacy 脚本
    script = CHINDA_LINE_SCRIPT if account == 'chinda' else NESTCAR_LINE_SCRIPT
    if not script.exists():
        return {'status': 'error', 'error': f'script not found: {script}'}

    r = subprocess.run([
        sys.executable, str(script),
        '--to', target,
        message
    ], capture_output=True, text=True, timeout=30)

    try:
        result = json.loads(r.stdout)
        return result
    except json.JSONDecodeError:
        return {
            'status': 'script_error', 'method': 'legacy_fallback',
            'returncode': r.returncode,
            'stdout': r.stdout[:500],
            'stderr': r.stderr[:500],
        }


def send_telegram(target: str, message: str, account: str = 'default') -> dict:
    """通过 openclaw 发送 Telegram"""
    cli = resolve_openclaw_bin()
    r = subprocess.run([
        cli, 'message', 'send',
        '--channel', 'telegram',
        '--account', account,
        '--target', target,
        '--message', message,
        '--json'
    ], capture_output=True, text=True, timeout=30)
    try:
        result = json.loads(r.stdout)
        return result
    except json.JSONDecodeError:
        return {
            'ok': r.returncode == 0,
            'returncode': r.returncode,
            'stdout': r.stdout[:500],
            'stderr': r.stderr[:500],
        }


def send_line_via_browser_fallback(day: str, target: str) -> dict:
    return {
        'ok': False,
        'status': 'browser_fallback_unavailable',
        'day': day,
        'target': target,
    }


def send(platform: str, target: str, message: str,
         account: str = None, auto_fallback: bool = True,
         day: str = None) -> dict:
    """
    统一发送入口
    platform: line | telegram
    target: 群ID / 用户chatId
    account: 平台账号
    auto_fallback: 主平台失败时是否尝试备选
    day: 用于 LINE browser fallback 的日期
    """
    if platform == 'line':
        # 尝试1: openclaw channel
        result = send_line_via_openclaw(target, message)
        if result.get('status') == 'ok':
            return {'platform': 'line', 'method': 'openclaw_channel', 'ok': True, 'result': result}

        # 尝试2: 直接走脚本
        script_account = account or 'nestcar'
        result = send_line_via_script(target, message, script_account)
        if result.get('ok'):
            return {'platform': 'line', 'method': 'direct_script', 'ok': True, 'result': result}

        # 尝试3: 429时走浏览器 fallback（发送到chindahotpot管理的群）
        http_429 = result.get('status') == 429 or '429' in str(result) or 'monthly limit' in str(result).lower()
        if http_429 and auto_fallback and CHINDA_LINE_SCRIPT.exists():
            use_day = day or datetime.now().strftime('%Y-%m-%d')
            fb_result = send_line_via_browser_fallback(use_day, target)
            if fb_result.get('ok'):
                return {'platform': 'line', 'method': 'browser_fallback', 'ok': True, 'result': fb_result}
            return {'platform': 'line', 'method': 'browser_fallback_attempted', 'ok': False, 'result': fb_result, 'api_error': str(result)}

        # fallback: 走 Telegram
        if auto_fallback:
            fb_result = send_telegram(target, message, account='default')
            return {'platform': 'line_fallback_telegram', 'method': 'fallback', 'ok': fb_result.get('ok', False), 'result': fb_result}
        else:
            return {'platform': 'line', 'ok': False, 'error': 'all line methods failed', 'api_result': result}

    elif platform == 'telegram':
        acct = account or 'default'
        result = send_telegram(target, message, acct)
        return {'platform': 'telegram', 'method': 'openclaw', 'ok': result.get('ok', False), 'result': result}

    else:
        return {'platform': platform, 'ok': False, 'error': f'unsupported platform: {platform}'}


def main():
    parser = argparse.ArgumentParser(description='Unified outbound gateway')
    sub = parser.add_subparsers(dest='action')

    # send command
    send_p = sub.add_parser('send')
    send_p.add_argument('--platform', required=True, choices=['line', 'telegram'])
    send_p.add_argument('--target', required=True, help='chatId / groupId')
    send_p.add_argument('--message', required=True)
    send_p.add_argument('--account', default=None)
    send_p.add_argument('--no-fallback', action='store_true', help='disable auto-fallback')

    # check command
    check_p = sub.add_parser('check')
    check_p.add_argument('--platform', choices=['line', 'telegram', 'all'], default='all')

    args = parser.parse_args()

    if args.action == 'send':
        result = send(args.platform, args.target, args.message,
                      account=args.account, auto_fallback=not args.no_fallback)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result.get('ok') else 1)

    elif args.action == 'check':
        print("=== LINE Accounts ===")
        line_status = check_line_status()
        for name, info in line_status.items():
            icon = '✅' if info['enabled'] and info['has_token'] else '❌'
            print(f"  {icon} {name}: enabled={info['enabled']}, token={info['token_len']}chars, botId={info['bot_id']}")

        print("\n=== Telegram Accounts ===")
        config = load_config()
        tg = config.get('channels', {}).get('telegram', {}).get('accounts', {})
        for name, info in sorted(tg.items()):
            en = info.get('enabled') if info.get('enabled') is not None else True
            bt = info.get('botToken', '')[:8] + '...' if info.get('botToken') else 'not_set'
            print(f"  {'✅' if en else '❌'} {name}: enabled={en}, token_pref={bt}")

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
