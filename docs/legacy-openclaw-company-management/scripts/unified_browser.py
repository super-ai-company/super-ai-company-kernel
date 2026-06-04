#!/usr/bin/env python3
from __future__ import annotations

"""
统一浏览器操作入口 — 整合所有平台操作脚本

agent 不再自己判断"用什么平台脚本"，而是：
1. 查数据库找路由
2. 根据平台选对应的操作脚本
3. 执行

用法：
  python3 unified_browser.py execute --business nestcar --platform line --action send --target "Fujane" --message "你好"
  python3 unified_browser.py execute --business video-publisher --platform douyin --action upload --file /tmp/video.mp4
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SKILL_DB = BASE / 'scripts' / 'skill_accounts_db.py'
PLATFORMS = BASE / 'scripts' / 'platforms'

PLATFORM_MAP = {
    'line': PLATFORMS / 'line_oa_operations.py',
    'douyin': PLATFORMS / 'douyin_operations.py',
    'tiktok': PLATFORMS / 'tiktok_operations.py',
    'google': PLATFORMS / 'google_operations.py',
}

BROWSER_RUN = BASE / 'scripts' / 'browser_profile_run.py'


def _try_browser_init(acc: dict) -> dict:
    """对 unknown / login_required 的 profile 尝试浏览器初始化。"""
    profile_key = acc.get('profile_key', '')
    profile_path = acc.get('profile_path', '')
    if not profile_key:
        return {'initialized': False, 'error': 'no profile_key'}

    profile_dir = Path(profile_path) if profile_path else None
    if not profile_dir or not profile_dir.exists():
        try:
            if profile_dir:
                profile_dir.mkdir(parents=True, exist_ok=True)
            return {'initialized': False, 'need_shift_login': True, 'error': 'profile_dir_created_empty', 'profile_path': str(profile_dir) if profile_dir else ''}
        except Exception as e:
            return {'initialized': False, 'error': f'cannot create profile dir: {e}'}

    entry_url = acc.get('oa_entry_url') or acc.get('entry_url') or ''
    platform = (acc.get('platform') or '').lower()

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                headless=True,
                viewport={'width': 1200, 'height': 800},
                accept_downloads=False
            )
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(entry_url or 'about:blank', wait_until='domcontentloaded', timeout=45000)
                page.wait_for_timeout(5000)
                body = page.locator('body').inner_text(timeout=5000)

                if 'waiting_for_manual_login' in body or 'login_required' in body or 'manual_login_required' in body or '登录' in body or 'Log in' in body or 'Sign in' in body:
                    return {'initialized': False, 'need_shift_login': True, 'error': 'login_page_detected', 'profile_path': str(profile_dir)}

                return {'initialized': True, 'stdout': 'login valid'}
            except Exception as e:
                return {'initialized': False, 'error': str(e)}
            finally:
                ctx.close()
    except Exception as e:
        return {'initialized': False, 'error': str(e)}


def db_get(business: str, platform: str, action: str) -> dict:
    r = subprocess.run([sys.executable, str(SKILL_DB), 'get',
                        '--business', business,
                        '--platform', platform,
                        '--action', action],
                       capture_output=True, text=True, timeout=15)
    return json.loads(r.stdout)


def execute(business: str, platform: str, action: str, target: str = None, message: str = None, file_path: str = None):
    # 1. 查数据库确定路由
    acc = db_get(business, platform, action)
    if not acc.get('found'):
        return {'ok': False, 'error': f'业务 {business} 的 {platform}/{action} 未注册', 'account_result': acc}

    # 2. 检查登录态 — unknown 或 login_required 时先尝试初始化浏览器
    status = acc.get('login_status', 'unknown')

    if status in ('unknown', 'login_required'):
        # 尝试用浏览器初始化（不是全新打开，而是用已有 profile 打开目标站点检查登录态）
        init_result = _try_browser_init(acc)
        if init_result.get('initialized'):
            status = 'verified'
            acc['login_status'] = 'verified'
        elif init_result.get('need_shift_login'):
            return {'ok': False, 'need_shift_login': True, 'error': f'账号 {acc["account_label"]} 浏览器已创建但需手动登录一次', 'profile_path': init_result.get('profile_path', ''), 'init_result': init_result}
        else:
            return {'ok': False, 'error': f'账号 {acc["account_label"]} 初始化失败: {init_result.get("error", "")}', 'init_result': init_result}

    if status == 'disabled':
        return {'ok': False, 'error': f'账号 {acc["account_label"]} 已被禁用'}

    # 3. 找对应的平台操作脚本
    # 映射数据库里的 platform 到脚本能找到的格式
    platform_map = {
        'line-official-manager': 'line',
        'line-official': 'line',
        'line': 'line',
        'douyin': 'douyin',
        'tiktok': 'tiktok',
        'google-business-profile': 'google',
        'google-cloud-console': 'google',
    }
    script_platform = platform
    for db_plat, script_plat in platform_map.items():
        if db_plat in platform or platform in db_plat:
            script_platform = script_plat
            break
    
    if script_platform not in PLATFORM_MAP:
        return {'ok': False, 'error': f'不支持平台操作: {platform} (映射到 {script_platform})'}

    op_script = None
    for key, path in PLATFORM_MAP.items():
        if key in platform.lower():
            op_script = path
            break
    
    if not op_script or not op_script.exists():
        return {'ok': False, 'error': f'{platform} 没有对应的操作脚本'}

    # 4. 调用平台脚本，传入 account_label（从数据库取的）
    cmd = [sys.executable, str(op_script), action]
    if target:
        cmd += ['--target', target]
    if message:
        cmd += ['--message', message]
    if file_path:
        cmd += ['--file', file_path]
    if business:
        cmd += ['--business', business]
    if acc.get('account_label'):
        cmd += ['--account', acc['account_label']]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return json.loads(r.stdout) if r.stdout else {'ok': False, 'stderr': r.stderr[:500]}


def main():
    parser = argparse.ArgumentParser(description='统一浏览器操作')
    sub = parser.add_subparsers(dest='command', required=True)

    exec_p = sub.add_parser('execute')
    exec_p.add_argument('--business', required=True)
    exec_p.add_argument('--platform', required=True, help='line, douyin, tiktok, google')
    exec_p.add_argument('--action', required=True, help='send, upload, reply, smoke')
    exec_p.add_argument('--target')
    exec_p.add_argument('--message')
    exec_p.add_argument('--file')

    args = parser.parse_args()
    result = execute(args.business, args.platform, args.action, args.target, args.message, args.file)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
