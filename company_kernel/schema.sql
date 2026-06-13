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


CREATE TABLE IF NOT EXISTS external_threads (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL DEFAULT '',
  external_user_id TEXT NOT NULL DEFAULT '',
  external_chat_id TEXT NOT NULL DEFAULT '',
  owner_agent TEXT NOT NULL DEFAULT '',
  bridge_agent TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  last_message_at TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS external_messages (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  direction TEXT NOT NULL DEFAULT '',
  platform TEXT NOT NULL,
  sender_kind TEXT NOT NULL DEFAULT '',
  sender_id TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  raw_excerpt TEXT NOT NULL DEFAULT '',
  evidence_path TEXT NOT NULL DEFAULT '',
  source_event_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(thread_id) REFERENCES external_threads(id)
);

CREATE TABLE IF NOT EXISTS external_ingest_cursors (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL DEFAULT '',
  bridge_agent TEXT NOT NULL DEFAULT '',
  cursor_value TEXT NOT NULL DEFAULT '',
  last_seen_at TEXT NOT NULL DEFAULT '',
  state_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_message_links (
  external_message_id TEXT NOT NULL,
  company_message_id TEXT NOT NULL DEFAULT '',
  conversation_message_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY(external_message_id, company_message_id, conversation_message_id)
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

CREATE TABLE IF NOT EXISTS task_workspaces (
  task_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL,
  manifest_path TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL,
  parent_task_id TEXT NOT NULL DEFAULT '',
  employee_id TEXT NOT NULL,
  artifact_type TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  mime_type TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL DEFAULT 'draft',
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'created',
  is_input INTEGER NOT NULL DEFAULT 0,
  is_output INTEGER NOT NULL DEFAULT 1,
  is_final INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '',
  checksum TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL DEFAULT '',
  employee_id TEXT NOT NULL,
  artifact_id TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL DEFAULT '',
  path_or_url TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  checksum TEXT NOT NULL DEFAULT '',
  is_final INTEGER NOT NULL DEFAULT 1,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handoffs (
  handoff_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  from_task_id TEXT NOT NULL,
  to_task_id TEXT NOT NULL,
  from_employee_id TEXT NOT NULL,
  to_employee_id TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  artifacts_json TEXT NOT NULL DEFAULT '[]',
  known_issues TEXT NOT NULL DEFAULT '',
  next_steps TEXT NOT NULL DEFAULT '',
  required_actions TEXT NOT NULL DEFAULT '',
  acceptance_notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'created',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_attempts (
  attempt_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL,
  employee_id TEXT NOT NULL,
  adapter_type TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  runtime TEXT NOT NULL DEFAULT '',
  pid TEXT NOT NULL DEFAULT '',
  session_key TEXT NOT NULL DEFAULT '',
  runtime_policy_json TEXT NOT NULL DEFAULT '{}',
  last_heartbeat_at TEXT NOT NULL DEFAULT '',
  last_progress_at TEXT NOT NULL DEFAULT '',
  cancel_requested_at TEXT NOT NULL DEFAULT '',
  supervisor_state_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS task_context_packages (
  context_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL,
  employee_id TEXT NOT NULL,
  context_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_sessions (
  session_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL DEFAULT '',
  attempt_id TEXT NOT NULL DEFAULT '',
  employee_id TEXT NOT NULL,
  adapter_type TEXT NOT NULL DEFAULT '',
  runtime_type TEXT NOT NULL DEFAULT '',
  pid TEXT NOT NULL DEFAULT '',
  session_key TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  started_at TEXT NOT NULL,
  last_heartbeat_at TEXT NOT NULL DEFAULT '',
  last_progress_at TEXT NOT NULL DEFAULT '',
  stopped_at TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS agent_tool_calls (
  tool_call_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL DEFAULT '',
  attempt_id TEXT NOT NULL DEFAULT '',
  employee_id TEXT NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  tool_name TEXT NOT NULL,
  tool_type TEXT NOT NULL DEFAULT 'other',
  input_summary TEXT NOT NULL DEFAULT '',
  input_json TEXT NOT NULL DEFAULT '{}',
  output_summary TEXT NOT NULL DEFAULT '',
  output_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'running',
  risk_level TEXT NOT NULL DEFAULT '',
  approval_id TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS budget_accounts (
  budget_account_id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL DEFAULT '',
  scope_id TEXT NOT NULL DEFAULT '',
  currency TEXT NOT NULL DEFAULT 'USD',
  soft_limit REAL NOT NULL DEFAULT 0,
  hard_limit REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS budget_events (
  budget_event_id TEXT PRIMARY KEY,
  budget_account_id TEXT NOT NULL DEFAULT '',
  trace_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL DEFAULT '',
  attempt_id TEXT NOT NULL DEFAULT '',
  employee_id TEXT NOT NULL DEFAULT '',
  cost_type TEXT NOT NULL DEFAULT '',
  amount REAL NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'USD',
  token_input INTEGER NOT NULL DEFAULT 0,
  token_output INTEGER NOT NULL DEFAULT 0,
  model_name TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT '',
  runtime_seconds INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
