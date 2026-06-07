# OpenClaw Company Bridge

This document defines how OpenClaw employees connect to Company Kernel without replacing OpenClaw's native communication system.

## Positioning

Company Kernel is the portable company coordination layer.

OpenClaw remains the native business-agent runtime.

The bridge is:

```text
Company Kernel task
-> company-openclaw-adapter
-> approved OpenClaw adapter execution
-> OpenClaw evidence / blocker
-> Company Kernel task status
-> short human-facing summary
```

This is not:

```text
Company Kernel directly edits OpenClaw sessions, bots, memories, hooks, or private runtime state.
```

## Supported Employees

OpenClaw employees may include:

- `main`
- `nestcar`
- `chindahotpot`
- `invest`
- `krothong`
- `video-creator`
- `video-ops`
- `video-publisher`

Each employee must be registered as its own Company Kernel employee with runtime `openclaw`.

## Required Contract

Every OpenClaw employee must follow the same durable flow:

1. Receive a Company Kernel task through `company-openclaw-adapter`.
2. ACK only as process state, never as completion.
3. Execute in its own OpenClaw workspace and business boundary.
4. Return either:
   - `completed` with evidence path; or
   - `blocked` with one concrete blocker and owner.
5. Main/human receives one short summary, not raw monitor counters.

## Commands

Dry-run bridge:

```bash
bin/company-openclaw-adapter --agent <agent-id>
```

Execute bridge:

```bash
bin/company-openclaw-adapter --agent <agent-id> --execute
```

`--execute` requires approval. If approval is missing, the adapter must create a pending approval and keep the task claimed. It must not write directly to OpenClaw internal bus.

## Human-Facing Reporting

Allowed summary style:

```text
完成了 chindahotpot 的 04店日报检查任务。
```

```text
nestcar 卡住：SMB share 根目录无权限，owner=main，下一步=修复服务端 ACL。
```

Forbidden summary style:

```text
inbox: 42 -> 0
done: 71 -> 218
failed: 0 -> 0
```

Raw counters may be written to reports, but should not be sent to normal human chats.

## Failure Handling

If OpenClaw native agent-to-agent tools fail with policy errors such as `unsupported_task_type`, `visibility restricted`, or `agent-to-agent denied`, do not keep retrying the same native path.

Instead:

1. Record the native failure as a blocker.
2. Send the task through Company Kernel direct message or task flow.
3. Return a clear blocked/completed result with evidence.

## Verification

Minimal bridge verification:

```bash
bin/companyctl employee create --id <agent-id> --name <name> --role business-agent --runtime openclaw --workspace <workspace>
bin/companyctl heartbeat --agent <agent-id>
bin/companyctl message direct --from main --to <agent-id> --body "只回复：<agent-id>_DIRECT_OK"
bin/company-openclaw-adapter --agent <agent-id>
```

Pass criteria:

- direct message produces sender-visible receipt;
- dry-run adapter writes evidence;
- execute mode refuses to bypass approval;
- no task is reported complete without evidence.
