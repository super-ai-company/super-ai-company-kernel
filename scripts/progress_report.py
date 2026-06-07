from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def build_payload(
    *,
    state: str,
    project: str,
    targets: str,
    action: str,
    checking: str = "",
    risks: str = "",
    blocked_on: str = "",
    tried: str = "",
    needs_action_from: str = "",
    task_id: str,
) -> dict:
    return {
        "ok": True,
        "task_id": task_id,
        "report": {
            "state": state,
            "project": project,
            "targets": targets,
            "action": action,
            "checking": checking,
            "risks": risks,
            "blocked_on": blocked_on,
            "tried": tried,
            "needs_action_from": needs_action_from,
            "created_at": now(),
        },
    }


def write_progress_report(
    *,
    out_dir: Path,
    state: str,
    project: str,
    action: str,
    checking: str = "",
    risks: str = "",
    blocked_on: str = "",
    tried: str = "",
    needs_action_from: str = "",
    targets: str | None = None,
    task_id: str = "",
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    resolved_task_id = task_id.strip() or f"direct-{stamp}"
    safe_task_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in resolved_task_id).strip("_") or "direct"
    path = out_dir / f"progress_{state}_{safe_task_id}_{stamp}.json"
    payload = build_payload(
        state=state,
        project=project,
        targets=targets or str(out_dir.resolve().parent),
        action=action,
        checking=checking,
        risks=risks,
        blocked_on=blocked_on,
        tried=tried,
        needs_action_from=needs_action_from,
        task_id=resolved_task_id,
    )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a repo-local progress report JSON.")
    parser.add_argument("--state", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--checking", default="")
    parser.add_argument("--risks", default="")
    parser.add_argument("--blocked_on", default="")
    parser.add_argument("--tried", default="")
    parser.add_argument("--needs_action_from", default="")
    parser.add_argument("--targets", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--out-dir", required=True, dest="out_dir")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = write_progress_report(
        out_dir=Path(args.out_dir),
        state=args.state,
        project=args.project,
        action=args.action,
        checking=args.checking,
        risks=args.risks,
        blocked_on=args.blocked_on,
        tried=args.tried,
        needs_action_from=args.needs_action_from,
        targets=args.targets or None,
        task_id=args.task_id,
    )
    print(json.dumps({"ok": True, "path": str(path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
