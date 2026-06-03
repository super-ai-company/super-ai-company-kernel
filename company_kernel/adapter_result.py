from __future__ import annotations

from pathlib import Path


def compact_output(path: Path, *, max_chars: int = 600) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact = "\n".join(lines[:8])
    if len(compact) > max_chars:
        return compact[: max_chars - 3].rstrip() + "..."
    return compact


def execution_detail(command: str, output: Path, *, exit_code: int = 0, success: bool = True) -> str:
    state = "completed" if success else f"failed exit_code={exit_code}"
    summary = compact_output(output)
    parts = [f"runtime execution {state}. command={command}", f"output={output}"]
    if summary:
        parts.extend(["", "Runtime output summary:", summary])
    return "\n".join(parts)
