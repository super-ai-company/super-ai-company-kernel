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
- If `agy` is present, `agy --print "只回复 ANTIGRAVITY_CLI_OK" --print-timeout 60s` must return the exact text before activation.
- If GUI execution is unavailable, the status must be `blocked` with a blocker and evidence path.
- If GUI execution is available, Antigravity must inspect the requested pages, implement or review, and return `status`, `changed_files`, `verification_run`, and `blocker` through `--complete`, `--block`, or a structured direct status reply.
- Any quota, login, app, CLI, or GUI-control error keeps the employee `candidate` and pauses autonomous routing.

## Required Checks

1. Locate app / CLI:
   - `/Applications/Antigravity.app`
   - Bundle id: `com.google.antigravity`
   - CLI: `agy`; verify with `command -v agy` and `agy --help`.
2. Register/update as candidate, not active:
   - `bin/companyctl employee create --id antigravity --name Antigravity --role developer --runtime antigravity --workspace <workspace>`
3. Smoke and activation:
   - `bin/companyctl heartbeat --agent antigravity`
- `bin/companyctl message direct --from main --to antigravity --body "只回复：antigravity_DIRECT_OK"`
- `agy --print "只回复 ANTIGRAVITY_CLI_OK" --print-timeout 60s` when the CLI exists.
- `bin/companyctl employee verify-direct --id antigravity --from main --rounds 3 --activate`
- `bin/company-antigravity-adapter --agent antigravity`

## Antigravity CLI Operation

Antigravity CLI is launched by typing `agy` directly in a terminal. Use PTY for interactive sessions.

Non-interactive smoke:

```bash
agy --print "只回复 ANTIGRAVITY_CLI_OK" --print-timeout 60s
```

Interactive / guided session:

```bash
agy
# or seed a goal and keep the session open
agy --prompt-interactive "请分析当前仓库 dashboard 前端，不要修改文件，先输出计划"
```

Inside an `agy` session:

- Bash mode: prefix shell commands with `!`, e.g. `!git status`, `!python3 -m unittest discover -s tests -v`.
- `/mcp`: manage/configure MCP servers.
- `/agents`: show active subagents.
- `/tasks`: monitor background tasks.
- `/context`: inspect context usage.
- `/btw`: side discussion / separate question.
- `/artifacts`: view implementation artifacts/plans.
- `/goal`: set current goal, e.g. `/goal Analyze README.md file`.

MCP config shared by Antigravity IDE/CLI:

```text
~/.gemini/config/mcp_config.json
```

For Google Developer Knowledge MCP, the codelab config uses an HTTP server URL like `https://developerknowledge.googleapis.com/mcp` with `X-Goog-Api-Key`. Never paste or store API keys in reports, skills, queue files, or prompts.

Company Kernel usage rule: first use `agy --print` and/or an interactive `agy` session to prove Antigravity can follow goals, run `!` shell checks, and return structured evidence. Only then route GUI/frontend implementation; otherwise keep it as reviewer/candidate.

## Execution Rules

- Default adapter mode is dry-run: writes a GUI task brief and evidence.
- Direct messages must not fail with “unsupported runtime”. They must write a direct GUI brief under `employees/antigravity/reports/direct/`, return an ACK to the sender, and send a structured `status: blocked` message back to the source when autonomous GUI execution is not verified.
- Progress/status receipts should use the normalized 5-layer protocol:
  `received`, `working`, `waiting`, `blocked`, `done`
  with preferred state names `acknowledged`, `actively_progressing`, `blocked_on_input_or_dependency`, `failed_to_progress`, `verified_complete`.
- 如果 heartbeat 进度从一层切到另一层，Kernel 会生成 repo 内 `progress.notification`，并把真实 delivery 结果回写给 dashboard/API；`pending` 是待发，`sent` 才是已送达。
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
- **Codex fallback rule:** until Antigravity proves the full evidence contract above, keep it out of autonomous frontend implementation. Use it only for GUI/page inspection briefs; route code changes to Codex target/guided mode with `ui-ux-pro-max`, then have Hermes verify diff/tests/browser evidence. A reply like “I am currently running on Gemini …” is `blocked_invalid_dispatch`, not progress.
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

## Postmortem: 2026-06-04 Why Antigravity Was Not Continuously Controllable

Observed failure:

- Antigravity/agy could pass simple exact replies such as CLI/direct smoke.
- The system initially treated those simple replies, app presence, or GUI brief generation as enough readiness.
- Complex frontend/dashboard tasks did not reliably return `changed_files`, `verification_run`, `browser_check`, and blocker evidence.
- Some Antigravity replies were generic model/status prose or referenced stale Hermes/Codex permission context rather than the current task.
- A read-only dashboard check was close to useful, but it was not implementation completion.

Root causes:

1. GUI employees have two separate capabilities: communication and execution. Communication was proven; autonomous GUI implementation was not.
2. GUI brief ACK was confused with actual page inspection and code/browser evidence.
3. The adapter did not initially force implementation tasks to include concrete changed files plus verification evidence.
4. Stale conversation carry-over was not treated as an invalid dispatch.
5. There was no strict fallback rule to route frontend code changes to Codex when Antigravity could not prove execution.

Preventive rules for future installs/customers:

- Keep Antigravity `candidate` unless it completes 2-4 direct rounds and a real GUI/frontend execution loop.
- Exact smoke proves only communication, not implementation readiness.
- GUI/front-end implementation is accepted only with `changed_files`, `verification_run`, `browser_check`, and evidence paths.
- Generic model/status prose, stale context, or missing changed files means `blocked_invalid_dispatch`, not progress.
- Until Antigravity proves the evidence contract, use it for inspection/review only and route code changes to Codex with UI/UX design-system rules.

## Blocked Cases

Block when app is missing, GUI state cannot be verified, task requires hidden credentials, or no evidence can be attached.
