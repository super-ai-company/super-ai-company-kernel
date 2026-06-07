#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODEX_SKILL = ROOT / "skills" / "company-employee-codex" / "SKILL.md"
ANTIGRAVITY_SKILL = ROOT / "skills" / "company-employee-antigravity" / "SKILL.md"


CODEX_BLOCK = """## Codex Mode Router

Codex is not one generic executor. Main/Hermes must select the mode before dispatch and write it into the task card.

- **目标模式 / Target Mode**: clear bounded implementation or bugfix; one objective; one verification gate; exit after result.
- **计划模式 / Plan Mode**: unknown root cause, high-risk schema/API/security, unfamiliar code; read-only; no edits.
- **列队模式 / Queue Mode**: multiple backlog items; Hermes owns order, queue state, isolation, and verification between items.
- **引导模式 / Guided Mode**: long or drift-prone task; checkpoints; Hermes monitors process/log/diff and steers.

Required task card fields:

```text
模式：目标 / 计划 / 列队 / 引导
目标：one objective
上下文：repo, branch, relevant facts
允许范围：paths/files/tools
禁止事项：secrets, production writes, unrelated refactors, destructive ops
验收标准：exact commands/tests/browser checks
输出格式：changed_files, commands_run, verification_run, evidence_path, blocker, risks
停止条件：when to stop/report/ask
```

## Codex CLI Patterns

Planning, read-only:

```bash
codex exec -C /absolute/repo -s read-only '计划模式：只读分析，禁止修改。目标：<goal>。输出 relevant files, plan, risks, verification gates。'
```

Bounded implementation:

```bash
codex exec --full-auto -C /absolute/repo '目标模式：完成 <goal>。允许范围：<paths>。禁止：<forbidden>。验收：<commands>。输出 changed_files/verification/blocker。'
```

Frontend/UI implementation must include UI/UX Pro Max:

```text
前端 UI/UX 强制规则：
1. 先读取 /Users/owner/.codex/skills/ui-ux-pro-max/SKILL.md。
2. 在目标项目运行：python3 /Users/owner/.codex/skills/ui-ux-pro-max/scripts/search.py "<product style>" --design-system --persist -p "<Project>" -f markdown
3. 具体页面加 --page "<page-name>"。
4. 实现前读取 design-system/MASTER.md 和 page override。
5. 输出设计系统路径、修改文件、验证命令、截图/DOM/browser 检查结果。
```

## Hermes Acceptance Gate for Codex

Main must verify, not trust claims:

```bash
git status --short
git diff --stat
git diff -- <relevant paths>
python3 -m unittest discover -s tests -v
```

Completion requires:

- expected files changed only;
- tests/verification command exit_code 0;
- report under `reports/codex-runs/` or relevant project report dir;
- queue item updated only after Hermes verification;
- GitHub delivery is not claimed until commit/push/PR/merge evidence exists.

"""


ANTIGRAVITY_BLOCK = """## Antigravity Guided Frontend Workflow

Use Antigravity as an interactive GUI/frontend worker, not a blind `--print` one-shot, when real UI work is expected.

Startup patterns:

```bash
# exact CLI smoke
agy --print "只回复 ANTIGRAVITY_CLI_OK" --print-timeout 60s

# interactive guided session with repo as working directory
agy --prompt-interactive "请作为 Company Kernel 前端员工。先 /goal 分析 dashboard，不要修改文件，先用 !git status 和 !python3 -m unittest ... 验证环境，然后输出计划。"
```

Inside `agy` session:

- `/goal <goal>`: set current objective before work.
- `!git status`: verify branch/dirty state.
- `!python3 -m unittest ...`: run verification through Bash mode.
- `/artifacts`: inspect implementation plans/artifacts before accepting.
- `/tasks`: monitor background work.
- `/context`: check context pressure.
- `/mcp`: inspect/configure MCP servers, including Developer Knowledge MCP.

MCP config path shared by Antigravity IDE/CLI:

```text
~/.gemini/config/mcp_config.json
```

Google Developer Knowledge MCP codelab endpoint:

```text
https://developerknowledge.googleapis.com/mcp
```

Use `X-Goog-Api-Key` in the MCP config only; never write API keys into skills/reports/queues/prompts.

## Antigravity Evidence Contract

For implementation tasks, Antigravity must return structured evidence:

```text
status: done | blocked | in_progress
current_action: ...
changed_files: [paths] or -
verification_run: command + exit_code + stdout/stderr summary
browser_check: page/DOM/screenshot evidence or blocker
artifacts: /path or /artifacts reference
blocker: empty or exact reason
next_action: ...
```

Acceptance rules:

- `agy --print` exact ACK proves CLI presence only.
- A model identity/status sentence is `blocked_invalid_dispatch`, not progress.
- GUI brief generation is candidate evidence, not implementation completion.
- If Antigravity can inspect UI but cannot write code, use it as reviewer and route implementation to Codex target/guided mode.
- If it can write code, Hermes still verifies git diff, tests, and browser/DOM evidence before queue completion.

"""


def insert_before_execution_rules(text: str, marker: str, block: str) -> tuple[str, bool]:
    if marker in text:
        return text, False
    anchor = "## Execution Rules\n"
    if anchor not in text:
        raise SystemExit(f"anchor not found: {anchor.strip()}")
    return text.replace(anchor, block + anchor, 1), True


def main() -> int:
    parser = argparse.ArgumentParser(description="Harden project-owned employee skill contracts.")
    parser.add_argument("--check", action="store_true", help="report whether updates are needed without writing")
    args = parser.parse_args()

    codex = CODEX_SKILL.read_text(encoding="utf-8")
    antigravity = ANTIGRAVITY_SKILL.read_text(encoding="utf-8")
    codex, codex_changed = insert_before_execution_rules(codex, "## Codex Mode Router", CODEX_BLOCK)
    antigravity = antigravity.replace(
        'agy --print "只回复 antigravity_CLI_OK" --print-timeout 60s',
        'agy --print "只回复 ANTIGRAVITY_CLI_OK" --print-timeout 60s',
    )
    antigravity, antigravity_changed = insert_before_execution_rules(
        antigravity,
        "## Antigravity Guided Frontend Workflow",
        ANTIGRAVITY_BLOCK,
    )
    changed = {
        str(CODEX_SKILL.relative_to(ROOT)): codex_changed,
        str(ANTIGRAVITY_SKILL.relative_to(ROOT)): antigravity_changed,
    }
    if not args.check:
        if codex_changed:
            CODEX_SKILL.write_text(codex, encoding="utf-8")
        if antigravity_changed:
            ANTIGRAVITY_SKILL.write_text(antigravity, encoding="utf-8")
    print(json.dumps({"ok": True, "check": args.check, "changed": changed}, ensure_ascii=False, indent=2))
    return 1 if args.check and any(changed.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
