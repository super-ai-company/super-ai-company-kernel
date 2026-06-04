#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


COMPANY_EMPLOYEES = {
    "antigravity",
    "claude",
    "codex",
    "hermes",
    "openclaw-main",
    "trae",
}


def openclaw_root() -> Path:
    env = os.environ.get("OPENCLAW_ROOT")
    if env:
        return Path(env).expanduser()
    if Path("/Users/owner/openclaw").exists():
        return Path("/Users/owner/openclaw")
    return Path.home() / "openclaw"


def project_root() -> Path:
    env = os.environ.get("OPENCLAW_COMPANY_MANAGEMENT_ROOT")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-") or "approval-task"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def decode_payload(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return value


def task_markdown(approval: dict[str, Any], approval_path: Path) -> str:
    task_id = str(approval.get("task_id") or approval_path.stem)
    source_agent = str(approval.get("source_agent") or "unknown")
    status = str(approval.get("status") or "unknown")
    payload = decode_payload(approval.get("payload"))
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2) if not isinstance(payload, str) else payload
    return f"""# Approval Follow-up: {task_id}

## Objective
Continue the Codex-side work after an OpenClaw Telegram approval was landed.

## Approval
- task_id: `{task_id}`
- source_agent: `{source_agent}`
- status: `{status}`
- approved_by: `{approval.get("approved_by") or ""}`
- approved_at: `{approval.get("approved_at") or ""}`
- approval_file: `{approval_path}`

## Payload

```json
{payload_text}
```

## Required Codex Response
1. Verify this approval reached the Codex-side queue.
2. Produce a completion receipt with evidence.
3. Do not start any independent Telegram Bot API polling watcher.

## Verdict
pending
"""


def sync_one(
    approval_path: Path,
    *,
    codex_queue_dir: Path,
    bus_dir: Path,
    force: bool,
) -> dict[str, Any]:
    approval = load_json(approval_path)
    task_id = str(approval.get("task_id") or approval_path.stem)
    source_agent = str(approval.get("source_agent") or "").strip()
    if source_agent not in COMPANY_EMPLOYEES:
        return {
            "task_id": task_id,
            "source_agent": source_agent,
            "status": "skipped",
            "reason": "source_agent_not_company_employee",
        }
    if str(approval.get("status") or "").strip() != "approved":
        return {
            "task_id": task_id,
            "source_agent": source_agent,
            "status": "skipped",
            "reason": "approval_status_not_approved",
        }

    codex_queue_dir.mkdir(parents=True, exist_ok=True)
    queue_file = codex_queue_dir / f"approval-{safe_name(task_id)}.md"
    wrote_task = False
    if force or not queue_file.exists():
        queue_file.write_text(task_markdown(approval, approval_path))
        wrote_task = True

    receipt = {
        "task_id": task_id,
        "source_agent": source_agent,
        "status": "approval_synced_to_codex_queue",
        "approval_file": str(approval_path),
        "codex_queue_file": str(queue_file),
        "created_at": now(),
        "telegram_button_landed": True,
        "codex_final_reply_proven": False,
        "note": "This proves approved-file to Codex queue sync only; final Telegram reply requires a separate completion receipt.",
    }
    done_dir = bus_dir / "done" / "codex"
    done_dir.mkdir(parents=True, exist_ok=True)
    receipt_file = done_dir / f"{safe_name(task_id)}.approval-synced.json"
    receipt_file.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return {
        "task_id": task_id,
        "source_agent": source_agent,
        "status": "synced",
        "wrote_task": wrote_task,
        "codex_queue_file": str(queue_file),
        "receipt_file": str(receipt_file),
    }


def approval_files(approvals_dir: Path, task_id: str | None) -> list[Path]:
    if task_id:
        path = approvals_dir / f"{safe_name(task_id)}.json"
        if path.exists():
            return [path]
        matches = sorted(approvals_dir.glob(f"*{safe_name(task_id)}*.json"))
        return matches
    return sorted(approvals_dir.glob("*.json"))


def main() -> None:
    root = openclaw_root()
    ap = argparse.ArgumentParser(description="Sync approved OpenClaw OPS approvals into the Codex task queue without Telegram polling.")
    ap.add_argument("--approvals-dir", default=str(root / "ops" / "approvals" / "approved"))
    ap.add_argument("--agent-bus", default=str(root / "ops" / "agent_bus"))
    ap.add_argument("--codex-queue-dir", default=str(project_root() / "codex-queue"))
    ap.add_argument("--task-id", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    approvals_dir = Path(args.approvals_dir).expanduser()
    files = approval_files(approvals_dir, args.task_id)
    results = [
        sync_one(
            path,
            codex_queue_dir=Path(args.codex_queue_dir).expanduser(),
            bus_dir=Path(args.agent_bus).expanduser(),
            force=args.force,
        )
        for path in files
    ]
    output = {
        "ok": True,
        "approvals_dir": str(approvals_dir),
        "processed": len(results),
        "synced": sum(1 for item in results if item.get("status") == "synced"),
        "results": results,
    }
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"processed={output['processed']} synced={output['synced']}")


if __name__ == "__main__":
    main()
