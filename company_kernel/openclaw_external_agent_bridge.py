from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
DEFAULT_OPENCLAW_ROOT = Path(os.environ.get("OPENCLAW_ROOT", "/Users/shift/openclaw")).resolve()
BRIDGE_AGENTS = {"codex": "codex", "antigravity": "antigravity", "agy": "antigravity"}


CommandRunner = Callable[[list[str]], tuple[int, str, str]]


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def default_runner(command: list[str]) -> tuple[int, str, str]:
    cp = subprocess.run(command, cwd=str(ROOT), text=True, capture_output=True)
    return cp.returncode, cp.stdout, cp.stderr


def safe_token(value: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(value or "").strip())
    return token.strip("-_") or "openclaw-task"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_json_output(raw: str) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "raw": raw}
    return parsed if isinstance(parsed, dict) else {"ok": False, "raw": parsed}


def bridge_report_path(task_id: str) -> Path:
    out = ROOT / "reports" / "openclaw-external-agent-bridge"
    out.mkdir(parents=True, exist_ok=True)
    return out / f"{safe_token(task_id)}.json"


def bus_root(openclaw_root: Path) -> Path:
    return openclaw_root / "ops" / "agent_bus"


def task_files(openclaw_root: Path, agent: str, limit: int) -> list[Path]:
    inbox = bus_root(openclaw_root) / "inbox" / agent
    if not inbox.exists():
        return []
    return sorted(path for path in inbox.glob("*.json") if path.is_file())[:limit]


def kernel_task_id(openclaw_task: dict, task_path: Path) -> str:
    raw = str(openclaw_task.get("company_kernel_task_id") or openclaw_task.get("kernel_task_id") or "")
    if raw:
        return raw
    return f"ocbridge-{safe_token(str(openclaw_task.get('task_id') or task_path.stem))}"


def openclaw_task_id(openclaw_task: dict, task_path: Path) -> str:
    return str(openclaw_task.get("task_id") or task_path.stem)


def payload_dict(task: dict) -> dict:
    payload = task.get("payload")
    return payload if isinstance(payload, dict) else {}


def task_title(task: dict, task_path: Path) -> str:
    payload = payload_dict(task)
    return str(
        task.get("title")
        or payload.get("title")
        or payload.get("summary")
        or payload.get("objective")
        or payload.get("instruction")
        or f"OpenClaw external agent task {openclaw_task_id(task, task_path)}"
    )[:240]


def task_description(task: dict, task_path: Path, kernel_agent: str) -> str:
    payload = payload_dict(task)
    skill_id = str(payload.get("skill_id") or payload.get("skill") or "")
    instruction = str(
        payload.get("instruction")
        or payload.get("message")
        or payload.get("objective")
        or payload.get("summary")
        or task.get("description")
        or ""
    )
    lines = [
        "OpenClaw delegated this task through the local external-agent bridge.",
        "",
        f"- openclaw_task_id: {openclaw_task_id(task, task_path)}",
        f"- source_agent: {task.get('source_agent') or task.get('source') or 'main'}",
        f"- target_agent: {task.get('target_agent') or task.get('target') or kernel_agent}",
        f"- managed_agent: {kernel_agent}",
    ]
    if skill_id:
        lines.append(f"- required_skill: {skill_id}")
    lines.extend(["", "## Instruction", "", instruction or task_title(task, task_path), "", "## Original OpenClaw Payload", "", json.dumps(task, ensure_ascii=False, indent=2)])
    return "\n".join(lines)


def task_priority(task: dict) -> str:
    priority = str(task.get("priority") or "P2").upper()
    return priority if priority in {"P1", "P2", "P3"} else "P2"


def source_agent(task: dict) -> str:
    source = str(task.get("source_agent") or task.get("source") or "main").strip()
    return source or "main"


@dataclass
class BridgeConfig:
    openclaw_root: Path
    agents: list[str]
    limit: int = 10
    execute: bool = False
    execute_adapter: bool = False
    adapter_timeout: int = 120
    by: str = "hermes"


def run_companyctl(args: list[str], runner: CommandRunner) -> tuple[int, dict, str]:
    code, out, err = runner([str(ROOT / "bin" / "companyctl"), *args])
    return code, parse_json_output(out), err


def ensure_kernel_task(task: dict, task_path: Path, kernel_agent: str, runner: CommandRunner) -> tuple[bool, str, dict]:
    task_id = kernel_task_id(task, task_path)
    show_code, show_payload, _ = run_companyctl(["task", "show", "--task-id", task_id], runner)
    if show_code == 0 and show_payload.get("ok"):
        return True, task_id, {"existing": True, "task": show_payload.get("task", {})}
    submit_code, submit_payload, submit_err = run_companyctl(
        [
            "task",
            "submit",
            "--from",
            source_agent(task),
            "--to",
            kernel_agent,
            "--title",
            task_title(task, task_path),
            "--description",
            task_description(task, task_path, kernel_agent),
            "--priority",
            task_priority(task),
            "--task-id",
            task_id,
        ],
        runner,
    )
    return submit_code == 0 and bool(submit_payload.get("ok")), task_id, {"existing": False, "payload": submit_payload, "stderr": submit_err[-1000:]}


def build_adapter_prompt(task: dict, task_path: Path, kernel_agent: str, kernel_task_id_value: str) -> str:
    payload = payload_dict(task)
    skill_id = str(payload.get("skill_id") or payload.get("skill") or "")
    required = [
        "You are controlled by OpenClaw through the local external-agent bridge.",
        f"managed_task_id: {kernel_task_id_value}",
        f"openclaw_task_id: {openclaw_task_id(task, task_path)}",
        f"target_agent: {kernel_agent}",
        "You must report concrete status, progress, blocker, verification and evidence.",
    ]
    if skill_id:
        required.append(f"Use Codex/agent skill if available: {skill_id}")
    return "\n".join(required + ["", "Task:", task_description(task, task_path, kernel_agent)])


def run_external_adapter(task: dict, task_path: Path, kernel_agent: str, kernel_task_id_value: str, config: BridgeConfig, runner: CommandRunner) -> dict:
    if not config.execute_adapter:
        report = bridge_report_path(kernel_task_id_value)
        write_json(
            report,
            {
                "ok": True,
                "mode": "adapter_dry_run",
                "kernel_task_id": kernel_task_id_value,
                "openclaw_task_id": openclaw_task_id(task, task_path),
                "agent": kernel_agent,
                "summary": "Bridge dry-run accepted OpenClaw task without starting external Codex/Agy runtime.",
                "created_at": now(),
            },
        )
        return {"ok": True, "status": "completed", "summary": "Bridge dry-run accepted OpenClaw task", "evidence": str(report), "adapter": {"dry_run": True}}

    prompt = build_adapter_prompt(task, task_path, kernel_agent, kernel_task_id_value)
    if kernel_agent == "codex":
        command = [
            str(ROOT / "bin" / "company-codex-adapter"),
            "--agent",
            "codex",
            "--direct-source",
            "openclaw",
            "--direct-session-key",
            kernel_task_id_value,
            "--direct-message",
            prompt,
            "--timeout",
            str(config.adapter_timeout),
        ]
    else:
        command = [
            str(ROOT / "bin" / "company-antigravity-adapter"),
            "--agent",
            "antigravity",
            "--direct-source",
            "openclaw",
            "--direct-session-key",
            kernel_task_id_value,
            "--direct-message",
            prompt,
            "--timeout",
            str(config.adapter_timeout),
        ]
    code, out, err = runner(command)
    payload = parse_json_output(out)
    ok = code == 0 and bool(payload.get("ok"))
    evidence = str(payload.get("workspace_progress_report") or payload.get("progress_report") or payload.get("report") or payload.get("brief") or "")
    summary = str(payload.get("reply") or payload.get("blocker") or payload.get("error") or ("adapter completed" if ok else "adapter blocked"))
    return {"ok": ok, "status": "completed" if ok else "blocked", "summary": summary[:2000], "evidence": evidence, "adapter": {"exit_code": code, "payload": payload, "stderr": err[-1000:]}}


def close_kernel_task(kernel_agent: str, kernel_task_id_value: str, adapter_result: dict, runner: CommandRunner) -> dict:
    if adapter_result["ok"]:
        evidence = str(adapter_result.get("evidence") or "")
        if not evidence:
            report = bridge_report_path(kernel_task_id_value)
            write_json(report, {"ok": True, "summary": adapter_result["summary"], "created_at": now()})
            evidence = str(report)
        code, payload, err = run_companyctl(["task", "done", "--agent", kernel_agent, "--task-id", kernel_task_id_value, "--summary", adapter_result["summary"], "--evidence", evidence], runner)
    else:
        code, payload, err = run_companyctl(["task", "block", "--agent", kernel_agent, "--task-id", kernel_task_id_value, "--blocker", adapter_result["summary"]], runner)
    return {"ok": code == 0 and bool(payload.get("ok")), "payload": payload, "stderr": err[-1000:]}


def move_openclaw_task(task_path: Path, task: dict, state: str, status: str, result: dict) -> Path:
    agent = str(task.get("target_agent") or task.get("target") or task_path.parent.name)
    target = task_path.parents[2] / state / agent / task_path.name
    updated = dict(task)
    updated.update(
        {
            "status": status,
            "updated_at": now(),
            "company_kernel_bridge": result,
            "evidence_path": result.get("adapter", {}).get("evidence") or result.get("evidence") or "",
        }
    )
    write_json(target, updated)
    try:
        task_path.unlink()
    except FileNotFoundError:
        pass
    return target


def process_task(task_path: Path, config: BridgeConfig, runner: CommandRunner = default_runner) -> dict:
    task = read_json(task_path)
    requested_agent = task_path.parent.name
    kernel_agent = BRIDGE_AGENTS.get(requested_agent, BRIDGE_AGENTS.get(str(task.get("target_agent") or task.get("target") or ""), ""))
    if not kernel_agent:
        return {"ok": False, "file": str(task_path), "error": f"unsupported bridge agent: {requested_agent}"}
    task["target_agent"] = requested_agent
    task_ok, ktid, submit_result = ensure_kernel_task(task, task_path, kernel_agent, runner)
    if not task_ok:
        result = {"ok": False, "file": str(task_path), "kernel_task_id": ktid, "status": "blocked", "error": "kernel task submit failed", "submit": submit_result}
        if config.execute:
            result["moved_to"] = str(move_openclaw_task(task_path, task, "failed", "blocked", result))
        return result
    adapter_result = run_external_adapter(task, task_path, kernel_agent, ktid, config, runner)
    close_result = close_kernel_task(kernel_agent, ktid, adapter_result, runner)
    ok = bool(adapter_result["ok"] and close_result["ok"])
    result = {
        "ok": ok,
        "file": str(task_path),
        "openclaw_task_id": openclaw_task_id(task, task_path),
        "kernel_task_id": ktid,
        "agent": kernel_agent,
        "status": "completed" if ok else "blocked",
        "adapter": adapter_result,
        "kernel_close": close_result,
    }
    if config.execute:
        state = "done" if ok else "failed"
        result["moved_to"] = str(move_openclaw_task(task_path, task, state, result["status"], result))
    return result


def run_bridge(config: BridgeConfig, runner: CommandRunner = default_runner) -> dict:
    results = []
    for agent in config.agents:
        for task_path in task_files(config.openclaw_root, agent, config.limit):
            results.append(process_task(task_path, config, runner))
    return {
        "ok": all(item.get("ok") for item in results) if results else True,
        "processed": len(results),
        "execute": config.execute,
        "execute_adapter": config.execute_adapter,
        "openclaw_root": str(config.openclaw_root),
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bridge OpenClaw agent_bus tasks to Company Kernel Codex/Agy adapters")
    parser.add_argument("--openclaw-root", default=str(DEFAULT_OPENCLAW_ROOT))
    parser.add_argument("--agents", default="codex,antigravity,agy")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--execute", action="store_true", help="move OpenClaw inbox files to done/failed after processing")
    parser.add_argument("--execute-adapter", action="store_true", help="start real Codex/Agy adapter execution; otherwise use bridge dry-run evidence")
    parser.add_argument("--adapter-timeout", type=int, default=120)
    parser.add_argument("--by", default="hermes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    agents = [agent.strip() for agent in args.agents.split(",") if agent.strip()]
    config = BridgeConfig(
        openclaw_root=Path(args.openclaw_root).expanduser().resolve(),
        agents=agents,
        limit=args.limit,
        execute=args.execute,
        execute_adapter=args.execute_adapter,
        adapter_timeout=args.adapter_timeout,
        by=args.by,
    )
    result = run_bridge(config)
    emit(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
