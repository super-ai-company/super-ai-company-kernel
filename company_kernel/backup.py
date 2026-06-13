"""Atomic backup/restore for the Company Kernel SQLite database.

Uses SQLite's online backup API (consistent even while the daemon/gateway hold the DB
open), keeps a rolling number of snapshots, and supports a guarded one-command restore.

CLI:
  company-backup snapshot [--keep N] [--label TEXT]
  company-backup list
  company-backup restore --from <snapshot.sqlite> [--yes]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
DB_PATH = Path(os.environ.get("COMPANY_KERNEL_DB_PATH", "") or (ROOT / "company.sqlite")).resolve()
BACKUP_DIR = Path(os.environ.get("COMPANY_KERNEL_BACKUP_DIR", "") or (ROOT / "state" / "backups")).resolve()


def now_stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def snapshot(keep: int = 14, label: str = "") -> dict:
    if not DB_PATH.exists():
        return {"ok": False, "error": f"database not found: {DB_PATH}"}
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"-{label}" if label else ""
    dest = BACKUP_DIR / f"company-{now_stamp()}{suffix}.sqlite"
    # Online backup API: consistent snapshot even with concurrent writers.
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    # integrity check on the snapshot
    chk = sqlite3.connect(str(dest))
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        chk.close()
    pruned = prune(keep)
    return {"ok": ok, "snapshot": str(dest), "size_bytes": dest.stat().st_size,
            "integrity": "ok" if ok else "FAILED", "kept": keep, "pruned": pruned}


def list_snapshots() -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob("company-*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)


def prune(keep: int) -> list[str]:
    snaps = list_snapshots()
    removed = []
    for old in snaps[max(keep, 1):]:
        removed.append(str(old))
        old.unlink(missing_ok=True)
    return removed


def restore(source: Path, yes: bool) -> dict:
    source = Path(source).expanduser().resolve()
    if not source.exists():
        return {"ok": False, "error": f"snapshot not found: {source}"}
    # validate snapshot before touching the live DB
    chk = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return {"ok": False, "error": "snapshot failed integrity check; refusing to restore"}
    finally:
        chk.close()
    if not yes:
        return {"ok": False, "error": "restore requires --yes (it overwrites the live database)",
                "would_restore_from": str(source), "into": str(DB_PATH)}
    # safety: back up current live DB before overwriting
    pre = None
    if DB_PATH.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        pre = BACKUP_DIR / f"pre-restore-{now_stamp()}.sqlite"
        shutil.copy2(DB_PATH, pre)
    shutil.copy2(source, DB_PATH)
    return {"ok": True, "restored_from": str(source), "into": str(DB_PATH),
            "pre_restore_backup": str(pre) if pre else ""}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Company Kernel SQLite backup/restore")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="create a consistent backup")
    s.add_argument("--keep", type=int, default=14)
    s.add_argument("--label", default="")
    sub.add_parser("list", help="list snapshots")
    r = sub.add_parser("restore", help="restore from a snapshot (overwrites live DB)")
    r.add_argument("--from", dest="source", required=True)
    r.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "snapshot":
        res = snapshot(keep=args.keep, label=args.label)
        emit(res)
        return 0 if res.get("ok") else 1
    if args.cmd == "list":
        snaps = list_snapshots()
        emit({"ok": True, "count": len(snaps), "backup_dir": str(BACKUP_DIR),
              "snapshots": [{"path": str(s), "size_bytes": s.stat().st_size,
                             "mtime": datetime.fromtimestamp(s.stat().st_mtime).isoformat(timespec="seconds")}
                            for s in snaps]})
        return 0
    if args.cmd == "restore":
        res = restore(Path(args.source), args.yes)
        emit(res)
        return 0 if res.get("ok") else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
