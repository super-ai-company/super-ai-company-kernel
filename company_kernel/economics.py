"""company_kernel.economics — pure unit-economics estimators, with NO dependency on companyctl, any
domain module, the DB (conn), config loaders, or the clock. The narrow cut the meeting admitted
(conv-20260620-135424-d487a5): just the two pure functions that turn already-fetched data + a pricing
dict into a classification / a cost number.

companyctl forwards these names with a plain `from .economics import ...` (no wrapper) — compute_economics
/ compute_cost_dashboard still call them through that forward. Pricing is estimation-only (no billing/
approval coupling), so these never read config: the caller passes the pricing-derived `pricing` / `rates`
dict in. The aggregators (compute_economics / compute_cost_dashboard / build_*) deliberately stay in
companyctl this batch — they eat `conn` + heartbeat/owner cross-module deps and get a layered split of
their own next batch (dashboard field semantics must not drift — owner's cost panel depends on them).
"""
from __future__ import annotations


def classify_task_type(title: str, description: str, pricing: dict) -> str:
    text = f"{title}\n{description}".lower()
    for ttype, keywords in (pricing.get("task_type_keywords") or {}).items():
        for kw in keywords:
            if kw.lower() in text:
                return ttype
    return "default"


def estimate_task_cost(ev: dict, rates: dict) -> float:
    """Cost of a task from its budget events: prefer recorded amount; else estimate from
    tokens; else fall back to runtime. Lets us compute margin even before token capture lands."""
    amount = float(ev.get("amount") or 0)
    if amount > 0:
        return amount
    ti = int(ev.get("token_input") or 0)
    to = int(ev.get("token_output") or 0)
    if ti or to:
        return ti / 1000.0 * float(rates.get("token_input_per_1k", 0)) + to / 1000.0 * float(rates.get("token_output_per_1k", 0))
    secs = int(ev.get("runtime_seconds") or 0)
    return secs / 60.0 * float(rates.get("runtime_per_minute", 0))
