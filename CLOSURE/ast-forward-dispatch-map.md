# CLOSURE/ast-forward-dispatch-map

AST 解析 companyctl ImportFrom(level=1)得 forward 再导出;refs=全仓裸名调用/属性访问;string_hits=字符串/动态派发命中

| module | symbol | refs | string_hits |
|---|---|---|---|
| approval | HIGH_RISK_APPROVAL_ACTIONS | 0 | 1 |
| approval | approval_control_summary | 4 | 3 |
| approval | approval_detail | 5 | 1 |
| approval | approval_is_high_risk | 3 | 1 |
| core | config | 99 | 11 |
| core | future_seconds | 3 | 1 |
| core | new_trace_id | 6 | 1 |
| core | now | 432 | 2 |
| core | parse_iso_datetime | 3 | 1 |
| core | parse_time | 27 | 1 |
| core | seconds_since | 12 | 1 |
| core.db | rows | 165 | 0 |
| core.events | audit | 85 | 4 |
| core.events | emit | 396 | 1 |
| core.events | record_event | 60 | 1 |
| core.events | trace_id_for_task | 20 | 2 |
| db_paths | ensure_db_parent | 13 | 0 |
| economics | build_cost_dashboard | 3 | 0 |
| economics | build_economics | 4 | 0 |
| economics | classify_task_type | 5 | 1 |
| economics | estimate_task_cost | 5 | 1 |
| notify | applescript_quote | 0 | 1 |
| notify | resolve_notification_target | 2 | 1 |
| notify | send_macos_notification | 2 | 2 |
| notify | send_slack_webhook | 3 | 2 |
| notify | send_telegram_notification | 3 | 2 |
| parsing | _openclaw_native_result_agent | 1 | 1 |
| parsing | _openclaw_native_result_evidence | 1 | 1 |
| parsing | _openclaw_native_result_summary | 1 | 1 |
| parsing | _openclaw_native_result_task_id | 2 | 1 |
| parsing | parse_json_arg | 21 | 1 |
| parsing | parse_json_output | 18 | 1 |
| parsing | parse_openclaw_agent_reply | 1 | 1 |
| progress | PROGRESS_TRANSITION_MESSAGES | 0 | 1 |
| progress | progress_notification_decision | 1 | 1 |
| progress | progress_notification_fingerprint | 2 | 1 |
| progress | progress_notification_message | 0 | 1 |
| schema_migrations | ensure_schema_migrations | 5 | 0 |
| textutil | clamp_audit_limit | 6 | 1 |
| textutil | communication_name_aliases | 3 | 1 |
| textutil | direct_probe_body | 1 | 1 |
| textutil | mermaid_node_id | 4 | 1 |
| textutil | normalize_employee_lookup | 4 | 1 |
| textutil | normalize_project | 3 | 1 |
| textutil | normalize_rfc | 5 | 1 |
| textutil | normalize_task_title | 4 | 1 |
| textutil | owner_action_next_step | 2 | 1 |
| textutil | parse_acceptance | 1 | 1 |
| textutil | parse_csv | 24 | 1 |
| textutil | parse_participants | 3 | 1 |
| textutil | parse_split_item | 1 | 1 |
| textutil | report_progress_task_id | 2 | 1 |
| textutil | safe_path_token | 3 | 1 |
| textutil | slug | 17 | 1 |
| watchdog | REAP_REASON_LABEL | 0 | 1 |
| watchdog | TERMINAL_TASK_STATUSES | 0 | 1 |
| watchdog | WATCHDOG_GLOBAL_CAP_SECONDS | 0 | 1 |
| watchdog | WATCHDOG_ORPHAN_GRACE_SECONDS | 0 | 1 |
| watchdog | cmd_watchdog_reap_stuck | 0 | 1 |
| watchdog | notify_owner_of_reaps | 3 | 1 |
| watchdog | process_alive | 3 | 1 |
| watchdog | reap_stuck_attempts_internal | 10 | 1 |

**总 forward 符号: 62 / 零引用: 0**