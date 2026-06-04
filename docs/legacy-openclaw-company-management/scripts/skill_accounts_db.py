#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path


def openclaw_root() -> Path:
    env = os.environ.get("OPENCLAW_ROOT")
    if env:
        return Path(env).expanduser()
    if Path("/Users/owner/openclaw").exists():
        return Path("/Users/owner/openclaw")
    return Path.home() / "openclaw"


def openclaw_workspace() -> Path:
    root = openclaw_root()
    workspace = Path(os.environ.get("OPENCLAW_WORKSPACE", root / "workspace-main")).expanduser()
    if "OPENCLAW_WORKSPACE" not in os.environ and Path("/Users/owner/openclaw/workspace-xmanx").exists():
        workspace = Path("/Users/owner/openclaw/workspace-xmanx")
    return workspace


WORKSPACE = openclaw_workspace()
DB_PATH = Path(os.environ.get("OPENCLAW_SKILL_ACCOUNTS_DB", WORKSPACE / "config" / "skill_accounts.db")).expanduser()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def get_route(conn: sqlite3.Connection, business: str, platform: str, action: str) -> dict | None:
    return row_to_dict(conn.execute(
        """
        SELECT *
        FROM skill_routes
        WHERE business = ?
          AND platform = ?
          AND action_type = ?
          AND is_active = 1
        ORDER BY priority ASC, id ASC
        LIMIT 1
        """,
        (business, platform, action),
    ).fetchone())


def get_account(conn: sqlite3.Connection, business: str, platform: str, route: dict | None) -> dict | None:
    skill = route.get("skill") if route else None
    if skill:
        row = conn.execute(
            """
            SELECT *
            FROM skill_accounts
            WHERE business = ?
              AND platform = ?
              AND skill = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (business, platform, skill),
        ).fetchone()
        if row:
            return row_to_dict(row)

    return row_to_dict(conn.execute(
        """
        SELECT *
        FROM skill_accounts
        WHERE business = ?
          AND platform = ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (business, platform),
    ).fetchone())


def cmd_get(args: argparse.Namespace) -> dict:
    if not DB_PATH.exists():
        return {"found": False, "error": "db_not_found", "db_path": str(DB_PATH)}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        route = get_route(conn, args.business, args.platform, args.action)
        account = get_account(conn, args.business, args.platform, route)
    finally:
        conn.close()

    if not route and not account:
        return {
            "found": False,
            "error": "not_found",
            "business": args.business,
            "platform": args.platform,
            "action": args.action,
            "db_path": str(DB_PATH),
        }

    result = {
        "found": True,
        "business": args.business,
        "platform": args.platform,
        "action": args.action,
        "route": route,
        "account": account,
        "db_path": str(DB_PATH),
    }
    if account:
        result.update(account)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Read OpenClaw skill account routes from SQLite.")
    sub = parser.add_subparsers(dest="command", required=True)

    get = sub.add_parser("get")
    get.add_argument("--business", required=True)
    get.add_argument("--platform", required=True)
    get.add_argument("--action", required=True)

    args = parser.parse_args()
    if args.command == "get":
        print(json.dumps(cmd_get(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
