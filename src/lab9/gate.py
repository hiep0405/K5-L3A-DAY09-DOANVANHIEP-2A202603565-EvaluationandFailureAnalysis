"""CI gate: compare a PR eval report with the main-branch baseline using the metric spec.

    python -m lab9.gate --pr reports/eval_workflow.json --baseline reports/baseline_v1.json

exit 0 = merge, exit 1 = blocked. Safety metrics: max 0, no averaging away.
Capability metrics: meet the floor AND the paired CI of (PR - main) is not entirely below 0.
Thresholds may only get stricter on the same eval-set version (ratchet).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from lab8.console import utf8_stdio
from lab9 import stats
from lab9.eval import SPEC, load_spec, per_item
from lab9.graders import GRADERS


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.
    Must catch at least:
      - any metric value in pr["metrics"] that is NaN or inf
      - len(pr["rows"]) != manifest items x len(manifest seeds)   (dropped / duplicated rows)
      - the (item_id, seed) set of PR differs from the baseline's (cherry-picked seeds/items)
      - pr["evaluator_failures"] non-empty, or a row whose value for a graded spec metric is
        None (a crashed grader silently scores as "missing" and drops out of the mean)
      - base_spec given and a threshold in spec is LOOSER than in base_spec for the same
        eval-set version (ratchet: max may not rise, min may not fall, a blocking metric may
        not become non-blocking, a metric may not disappear; the same for spec["slices"],
        including dropping no_significant_drop)
      - an item's slice tags in PR rows differ from the baseline's (relabelling an item out of a
        slice hides its regression from the slice gate)"""
    problems: list[str] = []

    # 1. Any metric in pr["metrics"] is NaN or inf
    for k, v in pr.get("metrics", {}).items():
        if isinstance(v, (int, float)) and (math.isnan(v) or math.isinf(v)):
            problems.append(f"metric {k} is NaN or inf")

    # 2. len(pr["rows"]) != manifest items x len(manifest seeds)
    manifest = pr.get("manifest", {})
    expected_count = manifest.get("items", 0) * len(manifest.get("seeds", []))
    if len(pr.get("rows", [])) != expected_count:
        problems.append(f"row count {len(pr.get('rows', []))} != expected {expected_count}")

    # 3. (item_id, seed) set differs from baseline
    pr_keys = {(r.get("item_id"), r.get("seed")) for r in pr.get("rows", [])}
    base_keys = {(r.get("item_id"), r.get("seed")) for r in base.get("rows", [])}
    if pr_keys != base_keys:
        problems.append("PR (item_id, seed) set differs from baseline")

    # 4. evaluator_failures non-empty, or row metric is None
    if pr.get("evaluator_failures"):
        problems.append(f"{len(pr['evaluator_failures'])} evaluator failure(s)")
    for m in spec.get("metrics", []):
        mid = m["id"]
        if mid in GRADERS:
            for r in pr.get("rows", []):
                if r.get(mid) is None:
                    problems.append(f"row missing metric value for {mid}")
                    break

    # 5. Ratchet check: base_spec given and threshold is looser
    if base_spec is not None and spec.get("eval_set_version") == base_spec.get("eval_set_version"):
        base_metrics = {m["id"]: m for m in base_spec.get("metrics", [])}
        for m in spec.get("metrics", []):
            mid = m["id"]
            if mid in base_metrics:
                bm = base_metrics[mid]
                th = m.get("threshold", {})
                bth = bm.get("threshold", {})
                if "min" in bth:
                    if "min" not in th or th["min"] < bth["min"]:
                        problems.append(f"loosened threshold min for {mid}")
                if "max" in bth:
                    if "max" not in th or th["max"] > bth["max"]:
                        problems.append(f"loosened threshold max for {mid}")
                if bm.get("blocking") and not m.get("blocking"):
                    problems.append(f"demoted blocking metric {mid}")
                if bth.get("no_significant_drop") and not th.get("no_significant_drop"):
                    problems.append(f"dropped no_significant_drop for {mid}")
        spec_mids = {m["id"] for m in spec.get("metrics", [])}
        for bmid, bm in base_metrics.items():
            if bmid not in spec_mids and bm.get("blocking"):
                problems.append(f"dropped metric {bmid}")

        base_slices = {s["name"]: s for s in base_spec.get("slices", [])}
        for s in spec.get("slices", []):
            sname = s["name"]
            if sname in base_slices:
                bs = base_slices[sname]
                if bs.get("min") is not None and (s.get("min") is None or s["min"] < bs["min"]):
                    problems.append(f"loosened slice min for {sname}")
                if bs.get("blocking") and not s.get("blocking"):
                    problems.append(f"demoted blocking slice {sname}")
                if bs.get("no_significant_drop") and not s.get("no_significant_drop"):
                    problems.append(f"dropped no_significant_drop for slice {sname}")

    # 6. slice tags difference
    pr_slices = {r["item_id"]: tuple(sorted(r.get("slices") or [])) for r in pr.get("rows", [])}
    base_slices_map = {r["item_id"]: tuple(sorted(r.get("slices") or [])) for r in base.get("rows", [])}
    if pr_slices != base_slices_map:
        problems.append("item slice tags in PR differ from baseline")

    return problems


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        lies entirely below 0
    Return (blocked ids like "slice:money_movement/pass_rate", report lines)."""
    blocked: list[str] = []
    lines: list[str] = []
    bs = spec.get("bootstrap", {})

    for s in spec.get("slices", []):
        name = s["name"]
        metric = s["metric"]
        s_min = s.get("min")
        blocking = s.get("blocking", True)

        pr_slice_rows = [r for r in pr.get("rows", []) if name in (r.get("slices") or [])]
        if not pr_slice_rows:
            continue
        vals = [float(r[metric]) for r in pr_slice_rows if r.get(metric) is not None]
        val = sum(vals) / len(vals) if vals else 0.0

        verdict = "ok"
        note = ""
        if s_min is not None and val < s_min:
            verdict = "fail"

        if s.get("no_significant_drop"):
            a = per_item(pr, metric, slice_name=name)
            b = per_item(base, metric, slice_name=name)
            keys = sorted(set(a) & set(b))
            if keys:
                d, lo, hi = stats.paired_bootstrap(
                    [a[k] for k in keys],
                    [b[k] for k in keys],
                    n_resamples=bs.get("n_resamples", 1000),
                    rng=bs.get("rng", 2027),
                )
                note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
                if hi < 0:
                    verdict = "fail"

        if verdict != "ok" and blocking:
            blocked.append(f"slice:{name}/{metric}")
        label = {"ok": "ok"}.get(verdict, "BLOCK" if blocking else "warn")
        lines.append(f"  slice:{name:<16} {val:<8.4f} min={s_min!s:<34} {note:<28} {label}")

    return blocked, lines


def check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> tuple[list[str], list[str]]:
    if pr["manifest"]["eval_set_sha"] != base["manifest"]["eval_set_sha"]:
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    problems = integrity_check(pr, base, spec, base_spec)
    if problems:
        return ["integrity"], [f"  INTEGRITY {p}" for p in problems]
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for m in spec["metrics"]:
        mid, th, val = m["id"], m.get("threshold", {}), pr["metrics"].get(m["id"])
        verdict, note = "ok", ""
        if val is None:
            verdict = "missing"
        elif "max" in th and val > th["max"]:
            verdict = "fail"
        elif "min" in th and val < th["min"]:
            verdict = "fail"
        elif "max_increase" in th and base["metrics"].get(mid):
            if val > base["metrics"][mid] * (1 + th["max_increase"]):
                verdict = "fail"
        if th.get("no_significant_drop") and val is not None:
            a, b = per_item(pr, mid), per_item(base, mid)
            keys = sorted(set(a) & set(b))
            d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                               n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
            note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
            if hi < 0:
                verdict = "fail"
        if verdict != "ok" and m.get("blocking"):
            blocked.append(mid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if m.get("blocking") else "warn")
        lines.append(f"  {mid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    if spec.get("slices"):
        sb, sl = slice_check(pr, base, spec)
        blocked += sb
        lines += sl
    return blocked, lines


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.gate")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--base-spec", help="metric spec on main, for the ratchet check (M7)")
    args = ap.parse_args(argv)
    pr = json.loads(Path(args.pr).read_text(encoding="utf-8"))
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_spec = load_spec(Path(args.base_spec)) if args.base_spec else None
    spec = load_spec(Path(args.spec))
    blocked, lines = check(pr, base, spec, base_spec)
    print(f"gate: PR {pr['run_id']} vs main {base['run_id']} · eval set {pr['manifest']['eval_set_sha']}")
    for line in lines:
        print(line)
    if blocked:
        print(f"exit 1 — blocked: {', '.join(blocked)}")
        return 1
    print("exit 0 — merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
