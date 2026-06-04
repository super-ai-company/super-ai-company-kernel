---
name: company-employee-gui-antigravity
description: Use when onboarding or operating Google Antigravity as a Company Kernel GUI candidate/employee, including app launch, GUI task briefs, manual completion, and evidence/blocker reporting. GUI brief ACK is not enough for active status.
---

# Company Employee: Antigravity

Antigravity is a GUI/browser/IDE employee. Current local deployments may expose an `agy --print` CLI that can pass direct communication, but task completion still has to be evidence-driven.

## One-Sentence Onboarding

“Onboard Antigravity as a GUI/front-end developer, verify 3 direct rounds through `agy --print` when available, and only accept tasks as done when it returns changed files plus verification evidence.”

## Hire Antigravity Contract

Antigravity is a GUI-first candidate unless it can prove a real execution loop. Do not treat `ANTIGRAVITY_DIRECT_OK`, app existence, generated briefs, or unrelated status prose as employee readiness.

Required pass criteria before `active`:

- 2-4 direct rounds return real replies, not only saved inbox files.
- A direct request produces a sender-visible status message.
- If `agy` is present, `agy --print "只回复 antigravity_CLI_OK" --print-timeout 60s` must return the exact text before activation.
- If GUI execution is unavailable, the status must be `blocked` with a blocker and evidence path.
- If GUI execution is available, Antigravity must inspect the requested pages, implement or review, and return `status`, `changed_files`, `verification_run`, and `blocker` through `--complete`, `--block`, or a structured direct status reply.
- Any quota, login, app, CLI, or GUI-control error keeps the employee `candidate` and pauses autonomous routing.

## Required Checks

1. Locate app:
   - `/Applications/Antigravity.app`
   - Bundle id: `com.google.antigravity`
2. Register/update as candidate, not active:
   - `bin/companyctl employee create --id antigravity --name Antigravity --role developer --runtime antigravity --workspace <workspace>`
3. Smoke and activation:
   - `bin/companyctl heartbeat --agent antigravity`
- `bin/companyctl message direct --from main --to antigravity --body "只回复：antigravity_DIRECT_OK"`
- `agy --print "只回复 antigravity_CLI_OK" --print-timeout 60s` when the CLI exists.
- `bin/companyctl employee verify-direct --id antigravity --from main --rounds 3 --activate`
- `bin/company-antigravity-adapter --agent antigravity`

## Execution Rules

- Default adapter mode is dry-run: writes a GUI task brief and evidence.
- Direct messages must not fail with “unsupported runtime”. They must write a direct GUI brief under `employees/antigravity/reports/direct/`, return an ACK to the sender, and send a structured `status: blocked` message back to the source when autonomous GUI execution is not verified.
- Direct GUI brief ACK keeps Antigravity as `candidate`; it does not prove active employee readiness.
- Active status is forbidden until `employee verify-direct` completes 2-4 rounds with receipt and the runtime has a real implementation/blocker evidence return path.
- If Antigravity cannot actually inspect the GUI pages and return implementation evidence, it must stay `candidate` and must not receive autonomous tasks.
- Any model quota, app, CLI, browser, or GUI execution error must mark the employee unavailable: `candidate` status plus paused communication until a new 2-4 round verification succeeds.
- `--execute` only opens Antigravity; it does not prove task completion.
- Use `--complete --task-id <id> --summary ... --evidence ...` or equivalent Company Kernel task completion after GUI work has real evidence.
- Use `--block --task-id <id> --blocker ...` if the GUI cannot complete safely.
- Every received request must ACK or return a blocker reply to the sender.
- A frontend implementation is not complete unless `git diff --stat` shows the expected files and tests/browser checks were run. If the reply references unrelated Hermes/Codex tasks, treat it as `blocked_context_mismatch`, not done.
- Keep task prompts narrow: include the exact repo path, branch, allowed files, expected pages, and required verification commands. Reject stale conversation carry-over.
- Adapter enforcement: lightweight verification messages must return the exact requested token. Complex frontend tasks must return structured `status`, `current_action`, `changed_files`, `verification_run`, `browser_check`, and `blocker`; replies mentioning stale Hermes/permission tasks or claiming `done` without changed files and verification are automatically `blocked`.
- If asked to optimize UI, Antigravity must inspect every dashboard page in the browser before proposing or implementing changes: Overview, Tasks & Workflows, Projects & Plans, AI Employees, Governance, Logs & Events, Trace Telemetry.
- If Antigravity cannot operate the GUI or commit code, it must return a blocker to the requesting agent and suggest `@codex` or another active employee to implement the changes.
- On failure, include status, blocker, evidence path, and next action; suggest active `@agent` collaborators when helpful.
- Human-originated requests must return through the requesting agent so the human operator receives a clear update.

## Verified Candidate Smoke

Use this smoke after configuring Antigravity:

```bash
bin/companyctl message direct \
  --from main \
  --to antigravity \
  --body "请查看每个 dashboard 页面并给出前端优化。只回复 ANTIGRAVITY_BRIEF_OK"
bin/companyctl message list --agent main
```

Expected current candidate behavior:

- direct command returns `activation_eligible=false`;
- `employees/antigravity/reports/direct/` contains a GUI brief and report;
- `main` receives a message containing `status: blocked`;
- blocker says GUI implementation still requires Antigravity app/human evidence.

This is a safe candidate smoke, not activation evidence.

## Activation Failure Handling

If Antigravity cannot return real GUI evidence:

```bash
bin/companyctl employee update --id antigravity --status candidate
bin/companyctl employee communication antigravity --enabled false
```

Then suggest active collaborators such as `@codex` for implementation or `@hermes` for verification.

## Blocked Cases

Block when app is missing, GUI state cannot be verified, task requires hidden credentials, or no evidence can be attached.
