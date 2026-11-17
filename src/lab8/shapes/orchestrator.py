"""Shape C — orchestrator plus read-only investigators.

The orchestrator delegates a structured order and policy check through the
typed ``handoff.v1`` contract. The investigator only receives read tools. The
host remains the sole writer and validates every quote before it can reach the
ledger.

Trace contract v1 (docs/contracts.md): return a trace_path to a JSON file
written in this run, with a matching run_id and an events list, on every
return path. Only the sanitized handoff and allowlisted tool results belong in
the trace; never record a raw claim, order, ticket, secret, or header.
"""

from __future__ import annotations

import json
import sqlite3

from lab8.db import connect
from lab8.durability import issue_refund
from lab8.handoff import make_handoff
from lab8.mock_llm import complete
from lab8.runtime import run_readonly_investigators
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish, safe_dump
from lab8.tools import tool_cost
from lab8.trace import Trace


SHAPE = "orchestrator"
READ_TOOLS = [
    "lookup_order",
    "lookup_policy",
    "lookup_refund_history",
    "compute_compensation",
]


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    conn = connect(store_path)
    try:
        return _run(conn, claim_id, seed=seed, crash_at=crash_at)
    finally:
        conn.close()


def _run(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    seed: int,
    crash_at: str | None,
) -> RunResult:
    budget = Budget()
    trace = Trace(make_run_id(SHAPE, claim_id, seed))
    budget.add_tool(tool_cost())
    trace.add("orchestrator", {"action": "start"})

    # Read only the fields the orchestrator needs. In particular, avoid
    # selecting ticket, parent_secret, or raw_headers into parent context.
    claim_row = conn.execute(
        "SELECT claim_id, principal_id, order_id FROM claims WHERE claim_id = ?",
        (claim_id,),
    ).fetchone()
    if claim_row is None:
        trace.add("error", {"reason": "unknown_claim"})
        return finish(
            conn,
            shape=SHAPE,
            claim_id=claim_id,
            seed=seed,
            status="failed",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason="unknown_claim",
        )

    # The plan is based only on the contract's allowlisted fields. It does not
    # choose the quote or grant the investigator additional tools.
    handoff = make_handoff(
        request_id=f"REQ-{claim_id}",
        claim_id=claim_id,
        task="verify_owner_order_and_quote_current_policy",
        facts={},
        allowed_tools=READ_TOOLS,
    )
    trace.add("handoff", safe_dump(handoff))
    complete(
        budget,
        seed=seed,
        purpose="plan",
        prompt=json.dumps(handoff, sort_keys=True, ensure_ascii=False),
    )

    investigated = run_readonly_investigators(
        conn, budget, handoff, principal_id=claim_row["principal_id"]
    )
    claim_view = investigated.get("claim_view") or {}
    order_view = investigated.get("tools", {}).get("lookup_order") or {}
    policy_view = investigated.get("tools", {}).get("lookup_policy") or {}
    history_view = investigated.get("tools", {}).get("lookup_refund_history") or {}

    # Every value logged below comes from a read-tool allowlist. Avoid logging
    # parent context, the customer ticket, database rows, or principal_id.
    trace.add(
        "investigator",
        {
            "claim_view": {
                key: claim_view[key]
                for key in ("found", "claim_id", "order_id")
                if key in claim_view
            },
            "lookup_order": order_view,
            "policy_count": len(policy_view.get("policies") or []),
            "refund_history": {
                key: history_view[key]
                for key in ("visible", "reason", "refund_count", "refunded_vnd")
                if key in history_view
            },
        },
    )

    if not claim_row["order_id"]:
        status = "needs_info"
        proposal = 0
        error_reason = "missing_order_id"
        quote_payload = None
    elif not order_view.get("visible"):
        status = "escalated"
        proposal = 0
        error_reason = order_view.get("reason") or "order_not_visible"
        quote_payload = None
    else:
        quote_payload = investigated.get("tools", {}).get("compute_compensation")
        if not isinstance(quote_payload, dict) or "amount_vnd" not in quote_payload:
            trace.add("error", {"reason": "quote_unavailable"})
            return finish(
                conn,
                shape=SHAPE,
                claim_id=claim_id,
                seed=seed,
                status="failed",
                proposal_amount=0,
                budget=budget,
                trace=trace,
                error_reason="quote_unavailable",
            )
        proposal = int(quote_payload["amount_vnd"])
        trace.add("quote", safe_dump(quote_payload))
        if proposal <= 0:
            status = "denied"
            proposal = 0
            error_reason = quote_payload.get("reason")
        else:
            # The host checks current approval, ownership, amount caps,
            # idempotency, and crash behavior. Always pass the full quote.
            outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)

            if outcome["ok"]:
                status = "resolved"
                error_reason = None
            else:
                status = "failed" if outcome["conflict"] else "denied"
                error_reason = outcome["reason"]

    # The model may draft language from the safe decision only. Its output is
    # intentionally not used for policy, amount, or ledger decisions.
    complete(
        budget,
        seed=seed,
        purpose="draft",
        prompt=f"status={status}; proposal_vnd={proposal}",
    )
    trace.add("decision", {"status": status})
    return finish(
        conn,
        shape=SHAPE,
        claim_id=claim_id,
        seed=seed,
        status=status,
        proposal_amount=proposal,
        budget=budget,
        trace=trace,
        error_reason=error_reason,
    )
