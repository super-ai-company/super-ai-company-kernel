from __future__ import annotations

import os
import json
from pathlib import Path


def resolve_root(default_root: Path) -> Path:
    return Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", default_root)).expanduser().resolve()


def resolve_db_path(root: Path) -> Path:
    override = str(os.environ.get("COMPANY_KERNEL_DB_PATH", "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    config_path_raw = str(os.environ.get("COMPANY_KERNEL_CONFIG_PATH", "") or "").strip()
    config_path = Path(config_path_raw).expanduser() if config_path_raw else Path("~/.gemini/antigravity/company_kernel_config.json").expanduser()
    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict) and payload.get("database_path"):
            return Path(str(payload["database_path"])).expanduser().resolve()
    return root / "company.sqlite"


def ensure_db_parent(db_path: Path) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path
