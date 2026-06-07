from __future__ import annotations

import os
from pathlib import Path


def resolve_root(default_root: Path) -> Path:
    return Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", default_root)).expanduser().resolve()


def resolve_db_path(root: Path) -> Path:
    override = str(os.environ.get("COMPANY_KERNEL_DB_PATH", "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return root / "company.sqlite"


def ensure_db_parent(db_path: Path) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path
