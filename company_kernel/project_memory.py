"""Project Memory Bank — a shared, curated, per-project knowledge base.

The design (owner-approved): a **project** maps to a workspace/repo and has a per-project **memory
lead** (curator, default hermes). Every employee's key operations are **captured** as memory
entries (task done/blocked, decisions, diagnoses). The lead periodically **curates** — dedups,
supersedes stale entries, and rebuilds a coherent **digest** (the current truth). Whoever picks up
the project **reads the digest first** (injected into their prompt), so context is shared across all
employees instead of each one re-scanning. This is the long-term, cross-agent organizational memory;
it complements the per-runtime native session memory (short-term working memory).

Storage: the kernel's own SQLite (projects / project_memory tables) + the digest text on the project
row. No external DB. Pure stdlib so it stays dependency-free; all functions take an open `conn`.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone

_WORKSPACE_RE = re.compile(r"(?:工作区|workspace)\s*[:：]\s*(\S+)", re.IGNORECASE)

ENTRY_TYPES = {"decision", "fact", "blocker", "diagnosis", "convention", "risk", "evidence"}
_TYPE_HEADING = {
    "decision": "决策", "fact": "事实", "blocker": "阻塞", "diagnosis": "诊断",
    "convention": "约定", "risk": "风险", "evidence": "证据",
}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------- projects
def create_project(conn: sqlite3.Connection, *, project_id: str, name: str = "", workspace: str = "",
                   lead_agent: str = "hermes") -> dict:
    ts = now()
    conn.execute(
        """INSERT INTO memory_banks(id, name, workspace, lead_agent, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'active', ?, ?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name, workspace=excluded.workspace,
             lead_agent=excluded.lead_agent, updated_at=excluded.updated_at""",
        (project_id, name or project_id, workspace.rstrip("/"), lead_agent or "hermes", ts, ts),
    )
    conn.commit()
    return get_project(conn, project_id)


def get_project(conn: sqlite3.Connection, project_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM memory_banks WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn, "SELECT * FROM memory_banks ORDER BY updated_at DESC")


def resolve_project_for_workspace(conn: sqlite3.Connection, workspace: str) -> dict | None:
    """Pick the project whose workspace is the longest prefix of this task workspace, so a task in
    /repo/sub belongs to the /repo project. Returns None if nothing maps (then capture is skipped)."""
    ws = (workspace or "").rstrip("/")
    if not ws:
        return None
    best, best_len = None, -1
    for p in list_projects(conn):
        pw = (p.get("workspace") or "").rstrip("/")
        if pw and (ws == pw or ws.startswith(pw + "/")) and len(pw) > best_len:
            best, best_len = p, len(pw)
    return best


# ---------------------------------------------------------------- entries
def remember(conn: sqlite3.Connection, *, project_id: str, title: str, body: str = "",
             entry_type: str = "fact", author_agent: str = "", source_task_id: str = "",
             source_conversation_id: str = "", evidence_path: str = "", importance: int = 1) -> dict:
    if not get_project(conn, project_id):
        raise ValueError(f"unknown project: {project_id}")
    etype = entry_type if entry_type in ENTRY_TYPES else "fact"
    eid = f"mem-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    conn.execute(
        """INSERT INTO project_memory(id, project_id, author_agent, entry_type, title, body,
             source_task_id, source_conversation_id, evidence_path, importance, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
        (eid, project_id, author_agent, etype, title.strip(), body.strip(), source_task_id,
         source_conversation_id, evidence_path, int(importance), ts, ts),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM project_memory WHERE id = ?", (eid,)).fetchone())


def recall(conn: sqlite3.Connection, *, project_id: str, query: str = "", limit: int = 50,
           include_superseded: bool = False) -> list[dict]:
    status_clause = "" if include_superseded else "AND status = 'active'"
    items = _rows(
        conn,
        f"""SELECT * FROM project_memory WHERE project_id = ? {status_clause}
            ORDER BY importance DESC, created_at DESC LIMIT ?""",
        (project_id, max(1, int(limit))),
    )
    if query:
        q = query.lower()
        items = [it for it in items if q in (it.get("title", "") + " " + it.get("body", "")).lower()]
    return items


def capture_task_outcome(conn: sqlite3.Connection, task: dict, *, kind: str, summary: str = "",
                         blocker: str = "", evidence: str = "") -> dict | None:
    """Auto-capture hook: a task finishing in a project becomes a memory entry. kind: done|blocked.
    Resolved by the task's workspace; no project mapping → no-op (returns None)."""
    workspace = str(task.get("workspace") or "")
    if not workspace:
        wsrow = conn.execute("SELECT path FROM task_workspaces WHERE task_id = ?", (task.get("id"),)).fetchone()
        workspace = str(wsrow["path"]) if wsrow else ""
    project = resolve_project_for_workspace(conn, workspace)
    if not project:
        return None
    if kind == "blocked":
        etype, title, body, imp = "blocker", f"阻塞:{task.get('title', '')}", blocker, 3
    else:
        etype, title, body, imp = "evidence", str(task.get("title", "")), summary, 2
    return remember(
        conn, project_id=project["id"], title=title, body=body, entry_type=etype,
        author_agent=str(task.get("target_agent") or task.get("claimed_by") or ""),
        source_task_id=str(task.get("id") or ""), evidence_path=evidence, importance=imp,
    )


# ---------------------------------------------------------------- curation (the lead's job)
def build_digest(project: dict, entries: list[dict]) -> str:
    """Render the active entries into a coherent current-truth markdown, grouped by type."""
    lines = [f"# {project.get('name') or project['id']} — 项目记忆摘要",
             f"_主负责人 {project.get('lead_agent') or 'hermes'} · 更新于 {now()[:19]} · {len(entries)} 条活跃记忆_", ""]
    by_type: dict[str, list[dict]] = {}
    for e in entries:
        by_type.setdefault(e.get("entry_type", "fact"), []).append(e)
    for etype in ["decision", "convention", "diagnosis", "risk", "blocker", "fact", "evidence"]:
        group = by_type.get(etype)
        if not group:
            continue
        lines.append(f"## {_TYPE_HEADING.get(etype, etype)}")
        for e in sorted(group, key=lambda x: (-int(x.get("importance", 1)), x.get("created_at", ""))):
            who = f" ({e['author_agent']})" if e.get("author_agent") else ""
            lines.append(f"- **{e.get('title', '')}**{who}" + (f" — {e['body']}" if e.get("body") else ""))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def curate(conn: sqlite3.Connection, *, project_id: str, actor: str = "") -> dict:
    """The memory lead's pass: dedup (same type+title → keep newest, supersede older), then rebuild
    the digest from what's left. Deterministic — an LLM-smart pass can be layered on by dispatching
    to the lead agent, but this keeps the bank coherent with zero model calls."""
    project = get_project(conn, project_id)
    if not project:
        raise ValueError(f"unknown project: {project_id}")
    active = recall(conn, project_id=project_id, limit=1000)
    seen: dict[tuple, dict] = {}
    superseded = 0
    for e in sorted(active, key=lambda x: x.get("created_at", "")):  # oldest first
        key = (e.get("entry_type"), e.get("title", "").strip().lower())
        if key in seen:  # an older entry with the same type+title — supersede it by the newer one
            older = seen[key]
            conn.execute("UPDATE project_memory SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                         (e["id"], now(), older["id"]))
            superseded += 1
        seen[key] = e
    conn.commit()
    remaining = recall(conn, project_id=project_id, limit=1000)
    digest = build_digest(project, remaining)
    ts = now()
    conn.execute("UPDATE memory_banks SET digest=?, digest_updated_at=?, updated_at=? WHERE id=?",
                 (digest, ts, ts, project_id))
    conn.commit()
    return {"ok": True, "project_id": project_id, "curated_by": actor or project.get("lead_agent"),
            "superseded": superseded, "active_entries": len(remaining), "digest": digest}


def curate_all(conn: sqlite3.Connection, *, actor: str = "") -> dict:
    """Curate every active project that has memory newer than its last digest — so the daemon can
    run this each tick cheaply without rewriting unchanged digests."""
    curated = []
    for p in list_projects(conn):
        if p.get("status") != "active":
            continue
        newest = conn.execute("SELECT MAX(updated_at) AS m FROM project_memory WHERE project_id = ?", (p["id"],)).fetchone()["m"]
        if newest and newest > str(p.get("digest_updated_at") or ""):
            curate(conn, project_id=p["id"], actor=actor or p.get("lead_agent") or "")
            curated.append(p["id"])
    return {"ok": True, "curated": len(curated), "projects": curated}


def digest_block_for_task(conn: sqlite3.Connection, task: dict) -> str:
    """Consumption: a prompt-ready memory block for whatever project owns this task's workspace, so
    the employee reads the shared project memory before working. '' if the task isn't in a project."""
    workspace = str(task.get("workspace") or "")
    if not workspace:
        row = conn.execute("SELECT path FROM task_workspaces WHERE task_id = ?", (task.get("id"),)).fetchone()
        workspace = str(row["path"]) if row else ""
    digest = digest_for_workspace(conn, workspace)
    if not digest:
        return ""
    return ("\n\n---\n## 📚 项目记忆(全员共享 · 开工前先读)\n"
            "下面是本项目到目前为止沉淀的共享记忆(决策/约定/诊断/风险/已知阻塞)。"
            "请基于它工作,别重复已确认的事、别重犯已知坑:\n\n" + digest)


def digest_for_workspace(conn: sqlite3.Connection, workspace: str) -> str:
    """Consumption: the curated digest for whichever project owns this workspace (or '')."""
    project = resolve_project_for_workspace(conn, workspace)
    if not project:
        return ""
    return str(project.get("digest") or "")


_AUTO_ACTORS = {"auto", "auto-sweep", "memory-curator", "system", ""}


def capture_approval_decision(conn: sqlite3.Connection, *, metadata: dict, action: str, decision: str,
                              actor: str, reason: str = "") -> dict | None:
    """A real owner approval/denial is a decision worth remembering. Files into the project resolved
    from the approval's '工作区: <path>' directive. Skips auto-approvals (no human signal) and
    approvals not tied to any project."""
    if actor in _AUTO_ACTORS:
        return None
    text = f"{metadata.get('title', '')}\n{metadata.get('description', '')}"
    m = _WORKSPACE_RE.search(text)
    project = resolve_project_for_workspace(conn, m.group(1)) if m else None
    if not project:
        return None
    verb = {"approved": "批准", "denied": "否决"}.get(decision, decision)
    return remember(
        conn, project_id=project["id"], entry_type="decision", author_agent=actor, importance=3,
        title=f"审批{verb}:{action} → {metadata.get('target', '')}",
        body=(f"{metadata.get('title', '')}。" + (f"理由:{reason}" if reason else "")).strip(),
    )


def archive_entry(conn: sqlite3.Connection, *, entry_id: str, actor: str = "") -> dict | None:
    """Manual curation: retire an entry (noise / wrong / superseded). It drops out of recall + the
    next digest. Returns the project_id so the caller can re-curate, or None if the entry is gone."""
    row = conn.execute("SELECT project_id FROM project_memory WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        return None
    conn.execute("UPDATE project_memory SET status='archived', updated_at=? WHERE id=?", (now(), entry_id))
    conn.commit()
    return {"ok": True, "entry_id": entry_id, "project_id": row["project_id"], "archived_by": actor}


def digest_for_project(conn: sqlite3.Connection, project_id: str) -> str:
    p = get_project(conn, project_id) if project_id else None
    return str(p.get("digest") or "") if p else ""


def capture_meeting_conclusion(conn: sqlite3.Connection, *, project_id: str, title: str, conclusion: str,
                               conversation_id: str = "", synthesizer: str = "", mode: str = "meeting") -> dict | None:
    """Meeting → memory: a concluded conversation's synthesized 纪要/方案/决策 becomes a high-importance
    decision entry, so the meeting's output is remembered instead of evaporating. No-op without a
    project or an empty conclusion."""
    if not project_id or not conclusion.strip() or not get_project(conn, project_id):
        return None
    label = {"meeting": "会议纪要", "standup": "站会汇总", "discuss": "方案/决策"}.get(mode, "会议结论")
    return remember(
        conn, project_id=project_id, title=f"【{label}】{title}", body=conclusion.strip(),
        entry_type="decision", author_agent=synthesizer or "meeting",
        source_conversation_id=conversation_id, importance=3,
    )
