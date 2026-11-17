"""Shape A — deterministic workflow.

The workflow owns the order of operations and the compensation quote. The host
remains the only component that can authorize and commit a refund.
"""

from __future__ import annotations

import sqlite3

from lab8.db import connect
from lab8.durability import issue_refund
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import (
    compute_compensation,
    get_claim_view,
    lookup_order,
    tool_cost,
)
from lab8.trace import Trace


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    """Run the fixed claims flow without letting ticket text set the quote."""
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
    trace = Trace(make_run_id("workflow", claim_id, seed))

    # The tools expose only the fields needed by this flow. In particular, do
    # not trace the ticket, principal, raw headers, or the full database rows.
    budget.add_tool(tool_cost())
    claim = get_claim_view(conn, claim_id)
    if not claim.get("found"):
        trace.add("claim", {"found": False})
        return finish(
            conn,
            shape="workflow",
            claim_id=claim_id,
            seed=seed,
            status="failed",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason="unknown_claim",
        )

    order_id = claim.get("order_id")
    trace.add("claim", {"found": True, "order_id": order_id})
    if not order_id:
        trace.add("decision", {"status": "needs_info"})
        return finish(
            conn,
            shape="workflow",
            claim_id=claim_id,
            seed=seed,
            status="needs_info",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason="missing_order_id",
        )

    budget.add_tool(tool_cost())
    order_result = lookup_order(conn, order_id, claim["principal_id"])
    trace.add(
        "lookup_order",
        {
            "visible": bool(order_result.get("visible")),
            "reason": order_result.get("reason"),
            "order_id": order_id,
        },
    )
    if not order_result.get("visible"):
        return finish(
            conn,
            shape="workflow",
            claim_id=claim_id,
            seed=seed,
            status="escalated",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason=order_result.get("reason"),
        )

    # Quote only from the structured order and current policy. User supplied
    # delay claims in the ticket are untrusted and are intentionally ignored.
    order = order_result["order"]
    budget.add_tool(tool_cost())
    quote = compute_compensation(conn, order)
    proposal = int(quote["amount_vnd"])
    trace.add(
        "quote",
        {
            "amount_vnd": proposal,
            "reason": quote.get("reason"),
            "clause_ids": quote.get("clause_ids", []),
            "parts": quote.get("parts", {}),
        },
    )
    if proposal <= 0:
        return finish(
            conn,
            shape="workflow",
            claim_id=claim_id,
            seed=seed,
            status="denied",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason=quote.get("reason"),
        )

    # This is a host operation: it validates the claim's own approval,
    # idempotency, and the per-order cap transactionally.
    outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    trace.add(
        "issue_refund",
        {"ok": bool(outcome["ok"]), "reason": outcome["reason"]},
    )
    if not outcome["ok"]:
        status = "failed" if outcome["conflict"] else "denied"
        return finish(
            conn,
            shape="workflow",
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
            error_reason=outcome["reason"],
        )

    return finish(
        conn,
        shape="workflow",
        claim_id=claim_id,
        seed=seed,
        status="resolved",
        proposal_amount=proposal,
        budget=budget,
        trace=trace,
    )
