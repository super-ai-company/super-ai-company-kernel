CREATE TABLE IF NOT EXISTS employees (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  role TEXT NOT NULL,
  runtime TEXT NOT NULL,
  workspace TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employee_runtimes (
  runtime TEXT PRIMARY KEY,
  command TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'registered',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  id TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  source_agent TEXT NOT NULL,
  target_agent TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  priority TEXT NOT NULL DEFAULT 'P2',
  status TEXT NOT NULL DEFAULT 'submitted',
  claimed_by TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  evidence_path TEXT NOT NULL DEFAULT '',
  blocker TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_metadata (
  task_id TEXT PRIMARY KEY,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  goal TEXT NOT NULL DEFAULT '',
  owner_agent TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  acceptance_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_tasks (
  project_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(project_id, task_id)
);

CREATE TABLE IF NOT EXISTS project_plan_items (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  title TEXT NOT NULL,
  task_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'planned',
  owner_agent TEXT NOT NULL DEFAULT '',
  due_at TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_relations (
  parent_task_id TEXT NOT NULL,
  child_task_id TEXT NOT NULL,
  relation_type TEXT NOT NULL DEFAULT 'subtask',
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(parent_task_id, child_task_id)
);

CREATE TABLE IF NOT EXISTS project_acceptances (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  accepted_by TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  review_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rfcs (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  author_agent TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  target_paths_json TEXT NOT NULL DEFAULT '[]',
  reason TEXT NOT NULL DEFAULT '',
  file_path TEXT NOT NULL DEFAULT '',
  decision_by TEXT NOT NULL DEFAULT '',
  decision_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  source_agent TEXT NOT NULL,
  target_agent TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_by TEXT NOT NULL,
  participants_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  source_agent TEXT NOT NULL,
  body TEXT NOT NULL,
  evidence_path TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id)
);

CREATE TABLE IF NOT EXISTS locks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  resource_key TEXT NOT NULL UNIQUE,
  owner_agent TEXT NOT NULL,
  lease_until TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeats (
  agent_id TEXT PRIMARY KEY,
  runtime TEXT NOT NULL DEFAULT '',
  workspace TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'alive',
  last_seen_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  source_agent TEXT NOT NULL,
  action TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_events (
  id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  event_type TEXT NOT NULL,
  source_agent TEXT NOT NULL,
  task_id TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  processed_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS hook_action_runs (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  hook_id TEXT NOT NULL,
  action_index INTEGER NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(event_id, hook_id, action_index)
);

CREATE TABLE IF NOT EXISTS adapter_runs (
  id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  agent_id TEXT NOT NULL,
  task_id TEXT NOT NULL DEFAULT '',
  command TEXT NOT NULL DEFAULT '',
  ok INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  attempt INTEGER NOT NULL DEFAULT 1,
  next_retry_at TEXT NOT NULL DEFAULT '',
  result_json TEXT NOT NULL DEFAULT '{}',
  acknowledged_at TEXT NOT NULL DEFAULT '',
  acknowledged_by TEXT NOT NULL DEFAULT '',
  acknowledgement_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
