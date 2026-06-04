# Local Environment and Skills

This project must not assume the original author's local machine paths. Treat
Company Kernel as the coordination layer and discover every runtime through
environment variables, standard hidden directories, or explicit user paths.

## Default Install Layout

Recommended default layout:

```text
<openclaw-root>/
  company-kernel/
  workspace-main/
  workspace-<business-or-agent>/
```

If OpenClaw is not installed yet, use:

```text
$HOME/openclaw/company-kernel
```

Do not hard-code a user-specific path such as `/Users/<name>/...`.

## Root Discovery Order

Company Kernel root:

```text
OPENCLAW_COMPANY_KERNEL_ROOT
<openclaw-root>/company-kernel
$HOME/openclaw/company-kernel
```

OpenClaw root:

```text
OPENCLAW_ROOT
$HOME/.openclaw
$HOME/openclaw
```

Codex home:

```text
CODEX_HOME
$HOME/.codex
```

Hermes home:

```text
OPENCLAW_HERMES_WORKSPACE
HERMES_HOME
$HOME/.hermes
```

Other runtimes should follow the same rule: environment variable first, then
the tool's standard hidden directory, then an explicit path passed by the user.

## Windows Notes

Use the same logical variables. Platform-specific fallbacks:

```text
%USERPROFILE%\.openclaw
%USERPROFILE%\openclaw
%USERPROFILE%\.codex
%APPDATA%\Codex
%USERPROFILE%\.hermes
```

Use `%APPDATA%` for persistent user config and `%LOCALAPPDATA%` for local cache
or machine-specific state.

## Linux Notes

Prefer XDG locations when a tool supports them:

```text
$XDG_CONFIG_HOME/<tool>
$HOME/.config/<tool>
$HOME/.<tool>
```

## Skill Enablement Flow

1. Clone or install this repository into the Company Kernel root.
2. Export the root variables if the install is not in the default location:

```bash
export OPENCLAW_ROOT="$HOME/openclaw"
export OPENCLAW_COMPANY_KERNEL_ROOT="$OPENCLAW_ROOT/company-kernel"
```

3. Run a read-only discovery pass:

```bash
cd "$OPENCLAW_COMPANY_KERNEL_ROOT"
python3 skills/openclaw-local-agent-bootstrap/scripts/scan_install.py \
  --openclaw-root "$OPENCLAW_ROOT" \
  --kernel-root "$OPENCLAW_COMPANY_KERNEL_ROOT"
```

4. Apply discovered employees only as candidates:

```bash
python3 skills/openclaw-local-agent-bootstrap/scripts/scan_install.py \
  --openclaw-root "$OPENCLAW_ROOT" \
  --kernel-root "$OPENCLAW_COMPANY_KERNEL_ROOT" \
  --apply
```

5. Promote an employee only after direct communication succeeds for 2-4 rounds:

```bash
bin/companyctl employee verify-direct --id <agent-id> --from main --rounds 3 --activate
```

If direct communication fails, keep the employee as `candidate` or mark it
blocked/unavailable. Do not display an unverified tool as active.

## Local Skills

Project skills live under:

```text
skills/
```

Current skill families:

```text
company-employee-openclaw
company-employee-hermes
company-employee-codex
company-employee-claude
company-employee-trae
company-employee-antigravity
company-employee-local-runtime
openclaw-local-agent-bootstrap
```

Each skill should describe:

- how to discover the runtime;
- how to register the employee;
- how to verify direct communication;
- how to report blockers;
- what evidence is required before activation.

## Communication Rule

Every employee request must produce at least one reply:

```text
human -> routing employee -> target employee -> routing employee -> human
```

or:

```text
human -> main -> target employee -> main -> human
```

Receipt-only inbox writes are not enough. A failed route must send back a
blocker with exact error text, evidence path if available, and the next action.

## Public Repository Hygiene

Do not commit:

- personal absolute paths;
- local tokens, API keys, bot tokens, chat IDs, or phone numbers;
- generated dashboard state;
- local service logs;
- machine-specific queue or inbox contents.

Use placeholders such as:

```text
$HOME/openclaw
$OPENCLAW_ROOT
$OPENCLAW_COMPANY_KERNEL_ROOT
<agent-workspace>
```

instead of machine-specific values.

## Minimum Verification

Before pushing public project changes:

```bash
python3 -B -m unittest discover -s tests -v
bin/company-dashboard --variant advanced
```

For dashboard work, also open:

```text
http://127.0.0.1:8780/dashboard.html
```

and confirm the browser console has no JavaScript errors.
