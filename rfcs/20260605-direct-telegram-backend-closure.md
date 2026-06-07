# RFC: direct messages / external mirror / adapter progress backend closure

- id: `20260605-direct-telegram-backend-closure`
- author: Hermes main
- timestamp: 2026-06-05
- status: approved-by-owner-in-chat-for-autonomous-iteration

## Goal

Complete the backend contract for the next Company Kernel iteration in the development checkout only:

- dashboard-ready direct messages feed without hard-coded employee set
- sanitized Hermes/Telegram external mirror bridge contract
- adapter_runs / progress_report evidence binding

## Scope

Allowed development path:

`/Users/owner/openclaw/workspace-xmanx/projects/super-ai-company-kernel`

Protected files that may be changed under this RFC:

- `company_kernel/company_dashboard.py`
- `company_kernel/api_gateway.py`
- `company_kernel/companyctl.py`
- `company_kernel/schema.sql`
- `tests/test_company_kernel_core.py`
- `scripts/progress_report.py`
- `README.md`

## Non-goals

- No changes to active runtime `/Users/owner/openclaw/company-kernel`
- No restart/deploy
- No real Telegram token or secrets access
- No OpenClaw root config edits
- No Hermes config edits

## Verification

- `python3 -m unittest discover -s tests -v`
- targeted CLI/API smoke in workspace only
- git diff review

## Rollback

Revert the listed files and this RFC file. No production state mutation is part of this RFC.
