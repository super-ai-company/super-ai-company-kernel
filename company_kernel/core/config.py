"""company_kernel.core.config — pure config-file readers with NO dependency on companyctl or any
domain module, and crucially none on companyctl's path globals: every function takes the
caller-resolved file path and only reads + parses it.

First config cut (codex re-sequence + meeting-locked contract, conv-20260620-122635-1eadbf): just the
three pure JSON loaders. The path globals stay nailed to companyctl as mock-patch anchors (25 call
sites across the suite), and resolve_kernel_paths stays there too — companyctl keeps each old name as a
THIN wrapper that assembles the path (env / default resolution) and delegates here. So core never
reverse-imports companyctl, every existing path-global patch in the suite still hits, and the only
thing that moved out of companyctl is the JSON-parsing body.

Each loader preserves its ORIGINAL fallback semantics exactly — they are deliberately NOT unified:
  - global   : missing / JSONDecodeError / non-dict  -> {}
  - comms     : missing -> the open-policy default dict; bad JSON raises (unchanged, no except)
  - pricing   : missing / (JSONDecodeError, OSError)  -> {}
"""
from __future__ import annotations

import json
from pathlib import Path


def load_global_config(path: str | Path) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_communication_config(path: str | Path) -> dict:
    comm_path = Path(path)
    if not comm_path.exists():
        return {"policy": {"mode": "open"}, "aliases": {}, "employees": {}, "channels": {}}
    return json.loads(comm_path.read_text(encoding="utf-8"))


def load_pricing_config(path: str | Path) -> dict:
    pricing_path = Path(path)
    if not pricing_path.exists():
        return {}
    try:
        return json.loads(pricing_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
