"""Shape B — one bounded ReAct agent with a host-only refund writer.

The agent gathers a claim view, the owner's order, current policy, and refund
history before asking the code tool for the quote. It never chooses an amount
from ticket text and it cannot call a write tool. The host passes the complete
quote to ``issue_refund`` so the host can enforce approval and the per-order
cap atomically.
"""

from __future__ import annotations

import sqlite3

from lab8.db import connect
from lab8.durability import issue_refund
from lab8.mock_llm import complete
from lab8.runtime import run_react
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.trace import Trace


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
    trace = Trace(make_run_id("single", claim_id, seed))
    state: dict = {}

    def decide(observation: dict) -> dict:
        """Pick only read tools; all decisions about money stay in code/host."""
        previous_tool = observation.get("last_tool")
        result = observation.get("last_result") or {}
        action: dict

        if previous_tool is None:
            action = {"tool": "get_claim_view", "args": {"claim_id": claim_id}}
        elif previous_tool == "get_claim_view":
            # Keep ticket contents in memory only. They are untrusted input and
            # are neither authoritative for eligibility nor safe trace material.
            if not result.get("found"):
                action = {"final": {"status": "failed", "error_reason": "unknown_claim"}}
            elif not result.get("order_id"):
                action = {"final": {"status": "needs_info", "error_reason": "missing_order_id"}}
            else:
                state["principal_id"] = result["principal_id"]
                state["order_id"] = result["order_id"]
                action = {
                    "tool": "lookup_order",
                    "args": {"order_id": result["order_id"], "principal_id": result["principal_id"]},
                }
        elif previous_tool == "lookup_order":
            if not result.get("visible"):
                action = {
                    "final": {
                        "status": "escalated",
                        "error_reason": result.get("reason", "order_not_visible"),
                    }
                }
            else:
                state["order"] = result["order"]
                action = {"tool": "lookup_policy", "args": {}}
        elif previous_tool == "lookup_policy":
            action = {
                "tool": "lookup_refund_history",
                "args": {"order_id": state["order_id"], "principal_id": state["principal_id"]},
            }
        elif previous_tool == "lookup_refund_history":
            action = {
                "tool": "compute_compensation",
                "args": {"order": state["order"]},
            }
        elif previous_tool == "compute_compensation":
            action = {"final": {"status": "quote_ready", "quote": result}}
        else:
            action = {"final": {"status": "failed", "error_reason": "unexpected_tool_result"}}

        # MockLLM gives this shape a reproducible model-call/cost budget. The
        # prompt intentionally contains no ticket, order, or customer fields.
        stage = previous_tool or "start"
        complete(budget, seed=seed, purpose="single", prompt=f"claims reimbursement stage={stage}")
        if "tool" in action:
            trace.add("tool_call", {"name": action["tool"]})
        else:
            trace.add("agent_final", {"status": action["final"]["status"]})
        return action

    trace.add("shape", {"name": "single", "mode": "bounded_read_only_react"})
    result = run_react(
        conn,
        budget,
        observation={"claim_id": claim_id},
        decide=decide,
        max_steps=8,
        worker=False,
    )

    # Record only small, allowlisted result summaries. In particular, never
    # serialize the raw claim view, ticket, order row, or policy clauses.
    last = result.get("observation", {})
    tool_name = last.get("last_tool")
    tool_result = last.get("last_result") or {}
    if tool_name == "get_claim_view":
        trace.add("claim_view", {"found": bool(tool_result.get("found"))})
    elif tool_name == "lookup_order":
        trace.add("order_lookup", {"visible": bool(tool_result.get("visible")), "reason": tool_result.get("reason")})
    elif tool_name == "lookup_policy":
        trace.add("policy_lookup", {"current_policy_count": len(tool_result.get("policies") or [])})
    elif tool_name == "lookup_refund_history":
        trace.add("refund_history", {"visible": bool(tool_result.get("visible")), "refund_count": tool_result.get("refund_count", 0)})
    elif tool_name == "compute_compensation":
        trace.add(
            "quote",
            {
                "amount_vnd": int(tool_result.get("amount_vnd", 0)),
                "reason": tool_result.get("reason"),
                "clause_ids": list(tool_result.get("clause_ids") or []),
            },
        )

    final = result.get("final") or {}
    status = final.get("status", "failed")
    error_reason = final.get("error_reason")
    if status == "failed" and error_reason == "step_cap":
        trace.add("error", {"reason": "step_cap"})
        return finish(
            conn,
            shape="single",
            claim_id=claim_id,
            seed=seed,
            status="failed",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason="step_cap",
        )

    if status != "quote_ready":
        trace.add("decision", {"status": status})
        return finish(
            conn,
            shape="single",
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason=error_reason,
        )

    quote = final["quote"]
    proposal = int(quote.get("amount_vnd", 0))
    if proposal <= 0:
        trace.add("decision", {"status": "denied", "reason": quote.get("reason")})
        return finish(
            conn,
            shape="single",
            claim_id=claim_id,
            seed=seed,
            status="denied",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason=quote.get("reason"),
        )

    outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    if not outcome["ok"]:
        status = "failed" if outcome["conflict"] else "denied"
        trace.add("host_decision", {"status": status, "reason": outcome["reason"]})
        return finish(
            conn,
            shape="single",
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
            error_reason=outcome["reason"],
        )

    trace.add("host_decision", {"status": "resolved"})
    return finish(
        conn,
        shape="single",
        claim_id=claim_id,
        seed=seed,
        status="resolved",
        proposal_amount=proposal,
        budget=budget,
        trace=trace,
    )
