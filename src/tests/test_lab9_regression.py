"""Regression test for M3 trace case C-002@11."""

from pathlib import Path
import pytest

from lab8.benchmark import prepare_from_memory, run_trial
from lab8.db import connect, load_json, load_jsonl
from lab9 import variants
from lab9.graders import DbFactKept, RefundAmountCorrect

pytestmark = pytest.mark.student
WORLD = Path("data/public_world_state.json")
CLAIMS = Path("data/public_claims.jsonl")


def _run(shape: str, claim_id: str, seed: int, tmp_path: Path):
    variants.register()
    world = load_json(WORLD)
    claims = load_jsonl(CLAIMS)
    store = tmp_path / f"{shape}-{claim_id}-{seed}.sqlite"
    prepare_from_memory(store, world, claims)
    payload, error = run_trial(shape, claim_id, seed, store)
    assert error is None
    import json
    trace_path = Path(payload.get("trace_path") or "")
    events = json.loads(trace_path.read_text(encoding="utf-8")).get("events", []) if trace_path.is_file() else []
    conn = connect(store)
    try:
        from lab8.metrics import ledger_snapshot
        snapshot = ledger_snapshot(conn, claim_id)
    finally:
        conn.close()
    return {"snapshot": snapshot, "events": events, "result": payload, "unexpected_rows": 0, "blob": ""}


def test_regression_c002_fixed_has_no_override(tmp_path):
    out = _run("fixed", "C-002", 11, tmp_path)
    from pydantic_evals.evaluators import EvaluatorContext
    ctx = EvaluatorContext(name="c002_fixed", inputs={}, metadata=None, expected_output={"refund_vnd": 0},
                           output=out, duration=0.0, _span_tree=None, attributes={}, metrics={})
    assert DbFactKept().evaluate(ctx).value is True
    assert RefundAmountCorrect().evaluate(ctx).value is True


def test_regression_c002_seeded_regression_fails_override(tmp_path):
    out = _run("seeded_regression", "C-002", 11, tmp_path)
    from pydantic_evals.evaluators import EvaluatorContext
    ctx = EvaluatorContext(name="c002_seeded", inputs={}, metadata=None, expected_output={"refund_vnd": 0},
                           output=out, duration=0.0, _span_tree=None, attributes={}, metrics={})
    assert DbFactKept().evaluate(ctx).value is False
    assert RefundAmountCorrect().evaluate(ctx).value is False
