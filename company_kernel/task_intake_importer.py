from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from company_kernel import companyctl

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INCOMING = ROOT / "state" / "task-intake" / "incoming"
DEFAULT_PROCESSED = ROOT / "state" / "task-intake" / "processed"
DEFAULT_FAILED = ROOT / "state" / "task-intake" / "failed"


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def read_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    return payload


def required_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(f"missing required field: {'/'.join(keys)}")


def optional_text(payload: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def safe_archive_name(path: Path, target_dir: Path) -> Path:
    candidate = target_dir / path.name
    if not candidate.exists():
        return candidate
    stem = path.stem
    suffix = path.suffix
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return target_dir / f"{stem}-{stamp}{suffix}"


def write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.with_name(path.name + ".receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def normalize_metadata(payload: dict[str, Any], source_file: Path) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata = {**metadata}
    metadata.setdefault("intake_bridge", True)
    metadata.setdefault("intake_file", str(source_file))
    metadata.setdefault("intake_imported_at", now())
    return metadata


INTAKE_FALLBACK_SENDER = "owner-shift"


def _known_employee(conn, agent_id: str):
    """True if registered, False if definitively not, None if we can't tell (don't remap on None)."""
    try:
        return conn.execute("SELECT 1 FROM employees WHERE id = ?", (agent_id,)).fetchone() is not None
    except Exception:
        return None


def submit_payload(payload: dict[str, Any], source_file: Path) -> dict[str, Any]:
    source = required_text(payload, "from", "source", "source_agent")
    target = required_text(payload, "to", "target", "target_agent")
    title = required_text(payload, "title")
    description = optional_text(payload, "description", "body", "message", default="")
    priority = optional_text(payload, "priority", default="P2")
    task_id = optional_text(payload, "task_id", "id", default="")
    metadata = normalize_metadata(payload, source_file)
    conn = companyctl.connect()
    # External apps (e.g. "codex-app") aren't registered employees — don't hard-fail; record the
    # original and submit as the owner so the task still lands.
    if _known_employee(conn, source) is False:  # only remap when definitively unregistered
        metadata["intake_original_from"] = source
        source = INTAKE_FALLBACK_SENDER
    return companyctl.submit_task_internal(
        conn,
        source=source,
        target=target,
        title=title,
        description=description,
        priority=priority,
        task_id=task_id,
        metadata=metadata,
    )


def process_file(path: Path, *, processed: Path, failed: Path) -> dict[str, Any]:
    try:
        payload = read_payload(path)
        result = submit_payload(payload, path)
    except (Exception, SystemExit) as exc:  # submit guards raise SystemExit — don't crash the run
        failed.mkdir(parents=True, exist_ok=True)
        archived = safe_archive_name(path, failed)
        shutil.move(str(path), str(archived))
        receipt = {"ok": False, "source_file": str(path), "archived_file": str(archived), "error": str(exc), "imported_at": now()}
        write_receipt(archived, receipt)
        return {"ok": False, "file": str(path), "archived_file": str(archived), "error": str(exc)}

    processed.mkdir(parents=True, exist_ok=True)
    archived = safe_archive_name(path, processed)
    shutil.move(str(path), str(archived))
    receipt = {"ok": True, "source_file": str(path), "archived_file": str(archived), "result": result, "imported_at": now()}
    write_receipt(archived, receipt)
    return {"ok": True, "file": str(path), "archived_file": str(archived), "result": result}


def import_once(*, incoming: Path = DEFAULT_INCOMING, processed: Path = DEFAULT_PROCESSED, failed: Path = DEFAULT_FAILED, limit: int = 25) -> dict[str, Any]:
    incoming.mkdir(parents=True, exist_ok=True)
    processed.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in incoming.glob("*.json") if p.is_file())[:limit]
    results = [process_file(path, processed=processed, failed=failed) for path in files]
    return {
        "ok": not any(not item.get("ok") for item in results),
        "incoming": str(incoming),
        "processed_dir": str(processed),
        "failed_dir": str(failed),
        "seen": len(files),
        "imported": sum(1 for item in results if item.get("ok")),
        "failed": sum(1 for item in results if not item.get("ok")),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import task JSON files from local intake into Company Kernel ledger.")
    parser.add_argument("--incoming", type=Path, default=DEFAULT_INCOMING)
    parser.add_argument("--processed", type=Path, default=DEFAULT_PROCESSED)
    parser.add_argument("--failed", type=Path, default=DEFAULT_FAILED)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args(argv)
    result = import_once(incoming=args.incoming, processed=args.processed, failed=args.failed, limit=args.limit)
    emit(result)
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
