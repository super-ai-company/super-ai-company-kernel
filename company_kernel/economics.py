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


def build_cost_dashboard(ledger_rows, employee_rows, heartbeat_ages, pricing, *, off_duty_threshold: int = 15, days: int = 14) -> dict:
    """Pure core of the on-duty cost dashboard — the aggregation lifted verbatim from companyctl's
    compute_cost_dashboard, with every DB/clock/config/constant dependency hoisted into arguments so it
    is deterministic and testable:
      - ledger_rows      : the full budget_events rows (employee_id, amount, token_*, runtime_seconds, day)
      - employee_rows    : employees ALREADY filtered of human-owners by the shell (id, status)
      - heartbeat_ages   : {employee_id -> minutes since last heartbeat | None | float('inf')} (pre-fetched)
      - pricing          : load_pricing_config() result (cost_rates / currency)
      - off_duty_threshold / days : the OFF_DUTY_HEARTBEAT_MINUTES constant and trend window, passed in
    Behaviour is byte-identical to the old inline version (golden-pinned), including the deliberate
    quirks: by_day counts the FULL ledger (human-owner events included) while totals count only the
    filtered employees, and an age of None or inf renders as null / off-duty."""
    rates = (pricing.get("cost_rates") or {})
    currency = pricing.get("currency", "USD")
    spend: dict = {}
    by_day: dict = {}
    for ev in ledger_rows:
        cost = estimate_task_cost(ev, rates)
        s = spend.setdefault(ev["employee_id"], {"executions": 0, "tokens": 0, "cost": 0.0})
        s["executions"] += 1
        s["tokens"] += int(ev.get("token_input") or 0) + int(ev.get("token_output") or 0)
        s["cost"] += cost
        day = ev.get("day") or ""
        if day:
            d = by_day.setdefault(day, {"day": day, "executions": 0, "cost": 0.0})
            d["executions"] += 1
            d["cost"] += cost
    by_employee = []
    on_duty_free = 0
    for e in employee_rows:
        eid = e["id"]
        s = spend.get(eid, {"executions": 0, "tokens": 0, "cost": 0.0})
        age = heartbeat_ages.get(eid)
        on_duty = age is not None and age <= off_duty_threshold
        cost = round(s["cost"], 4)
        if on_duty and cost == 0:
            on_duty_free += 1
        by_employee.append({
            "employee_id": eid,
            "status": e.get("status", ""),
            "on_duty": on_duty,
            "heartbeat_age_minutes": None if age is None or age == float("inf") else round(age, 1),
            "executions": s["executions"],
            "tokens": s["tokens"],
            "cost": cost,
        })
    by_employee.sort(key=lambda x: (-x["cost"], -x["executions"], x["employee_id"]))
    trend = sorted(by_day.values(), key=lambda d: d["day"], reverse=True)[:days]
    for d in trend:
        d["cost"] = round(d["cost"], 4)
    return {
        "currency": currency,
        "by_employee": by_employee,
        "by_day": list(reversed(trend)),  # oldest→newest for charting
        "totals": {
            "cost": round(sum(x["cost"] for x in by_employee), 4),
            "executions": sum(x["executions"] for x in by_employee),
            "on_duty": sum(1 for x in by_employee if x["on_duty"]),
            "on_duty_free": on_duty_free,
            "employees": len(by_employee),
        },
        "note": "在岗=心跳15分钟内仍活跃(内部通信/查任务0花费);cost=budget_events 估算(amount>token>runtime);只有接单执行才计费。",
    }
