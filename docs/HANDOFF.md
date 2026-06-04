# Company Kernel Handoff

## Project Root

The real Company Kernel project root is:

```text
/Users/shift/openclaw/company-kernel
```

This is the repository that serves the current Operations Console, owns the SQLite state, and is pushed to:

```text
https://github.com/shiftshen/super-ai-company-kernel.git
```

The older workspace project:

```text
/Users/shift/openclaw/workspace-xmanx/projects/openclaw-company-management
```

is an independent OpenClaw control/skill project. It should stay separate and
must not be treated as the active Company Kernel root.

## Runtime Endpoints

Default local services:

```text
API Gateway:       http://127.0.0.1:8765
Dashboard Server:  http://127.0.0.1:8780/dashboard.html
RPC:               http://127.0.0.1:8766
gRPC:              http://127.0.0.1:8767
```

Port `3000` is intentionally not used by Company Kernel.

Local environment discovery, hidden tool directories, and skill activation are
documented in [LOCAL_ENVIRONMENT_AND_SKILLS.md](LOCAL_ENVIRONMENT_AND_SKILLS.md).

## Main Commands

```bash
cd /Users/shift/openclaw/company-kernel
bin/companyctl doctor --summary
bin/company-dashboard --variant advanced
python3 -B -m unittest discover -s tests -v
bin/company-local-smoke --json-only
```

## Dashboard Verification

After changing dashboard code:

```bash
cd /Users/shift/openclaw/company-kernel
bin/company-dashboard --variant advanced
node - <<'NODE'
const fs = require('fs');
const html = fs.readFileSync('state/dashboard.html', 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\btype=["'](?:application\/json|application\/ld\+json)["'])[^>]*>([\s\S]*?)<\/script>/gi)].map(m => m[1]);
let checked = 0;
for (const script of scripts) {
  const trimmed = script.trim();
  if (!trimmed) continue;
  new Function(trimmed);
  checked++;
}
console.log(`checked ${checked} ordinary inline scripts`);
NODE
```

Then open:

```text
http://127.0.0.1:8780/dashboard.html
```

and verify the browser console has zero JavaScript errors.

## Source Layout

```text
company_kernel/                 Python package and core business logic
bin/                            CLI entry points and service wrappers
dashboard_templates/            Version-controlled dashboard HTML template
config/                         Policy, communication, hooks, sandbox config
docs/                           Runbooks, goals, handoff docs
tests/                          unittest suite
state/                          Generated local runtime state, ignored by git
logs/                           Local service logs, ignored by git
employees/                      Managed employee runtime state, mostly ignored
```

## Git State

Current policy is one branch only:

```text
main
```

Before pushing:

```bash
git status --short --branch
python3 -B -m unittest discover -s tests -v
bin/company-dashboard --variant advanced
```

## Related Project Notes

The separate `openclaw-company-management` project contains OpenClaw-side helper scripts for:

- OpenClaw-side skill install
- account route SQLite helpers
- progress reports
- agent bus request helpers
- Company Kernel health bridge
- attendance and communication smoke scripts

Those files remain in their own repository. They are not the canonical Company Kernel runtime implementation. New Company Kernel code should be added to `company_kernel/`, `bin/`, `dashboard_templates/`, `config/`, or `docs/` in this repository.

## Ownership Boundary

Company Kernel owns:

- employee registry and status
- task lifecycle
- conversations and direct messages
- approvals and high-risk gates
- runtime adapter verification
- dashboard generation and API gateway

OpenClaw, Hermes, Codex, Claude, Trae, and Antigravity are runtime adapters or employees. They should not directly change kernel policy, protected paths, or state files without going through Company Kernel commands and approval gates.
