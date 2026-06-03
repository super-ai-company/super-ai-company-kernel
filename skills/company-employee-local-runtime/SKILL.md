---
name: company-employee-local-runtime
description: Use when onboarding a generic local, candidate, or third-party runtime employee into Company Kernel with safe discovery, candidate isolation, direct smoke, and blocked routing.
---

# Company Employee: Local Runtime

Use this for Cursor, Devin, GitHub Copilot, local models, or any employee without a dedicated adapter yet.

## One-Sentence Onboarding

“Discover this local runtime, register it as candidate until direct smoke and routing evidence pass, then promote to active only with owner confirmation.”

## Required Checks

1. Identify runtime command/app and workspace.
2. Read local files: `AGENTS.md`, profile/config docs, auth/status files if present.
3. Register as candidate first:
   - `bin/companyctl employee create --id <agent> --name <name> --role <role> --runtime local --workspace <path>`
   - Keep `status=candidate` until verified.
4. Smoke:
   - `bin/companyctl heartbeat --agent <agent>`
   - If no adapter exists, record manual evidence instead of claiming direct execution.
5. Promote only after owner confirms:
   - `bin/companyctl employee update <agent> --status active`

## Execution Rules

- Candidate employees must not be schedulable or appear as active workers.
- Do not assume browser/IDE login state transfers into automation.
- Do not invent adapter behavior. If no adapter exists, create a task brief and require manual completion evidence.
- Communication can be enabled only after direct reply channel and routing are known.

## Blocked Cases

Block when runtime identity is unclear, workspace is shared with unrelated projects, auth/login is missing, communication route is unknown, or no evidence source exists.
