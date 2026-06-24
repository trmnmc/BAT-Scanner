#!/usr/bin/env python3
"""Estimate evaluation — backtest the Stage 6B price ranges against REAL sold outcomes.

    python tools/evaluate_estimates.py [--comps data/comps.json] [--min-examples 8] [--json]

Each comp in the pool is a car that ACTUALLY sold for a known price. We treat each one as if it
were live at its sale time, build an estimate range from the OTHER sales available *before* it (no
lookahead), and check whether the real sale price landed inside the predicted band. Reports:

  - the share of sales that fell INSIDE their predicted range
  - midpoint error (how far the band's midpoint was from the actual price)
  - accuracy broken down by the estimate's confidence
  - accuracy by make/model where there are enough examples

Read-only and offline: it never fetches anything and never writes a data file. It reuses the SAME
estimate engine the pipeline uses (scraper.value + scraper.opportunity), so what it measures is what
ships.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import comps as comps_mod  # noqa: E402
from scraper import identity, opportunity, value  # noqa: E402


def _comp_to_car(c):
    """Rebuild a live-shaped car from a sold comp (a no-reserve sale at a known price). The bid is
    left None so the estimate is PURE comp inference, independent of the actual price we're testing."""
    cmake = c.get("canonical_make") or c.get("make")
    cmodel = c.get("canonical_model") or c.get("model")
    car = {
        "id": c.get("id"), "title": c.get("title", ""), "year": c.get("year"),
        "make": {"slug": c.get("make"), "name": c.get("make")},
        "models": [{"slug": c.get("model"), "name": c.get("model")}],
        "bid": {"amount": None, "currency": "USD"},
        "flags": {"no_reserve": True}, "engagement": {}, "details": None,
        "category_ids": c.get("category_ids", []),
        "_ends_ts": c.get("sold_ts"),
    }
    # reuse the annotated canonical identity so matching lines up with the pipeline
    car["vehicle_identity"] = {"canonical_make": cmake, "canonical_model": cmodel,
                               "confidence": "high" if cmake and cmodel else "low",
                               "originality": None}
    return car


def evaluate(pool, *, min_examples=8):
    annotated = [identity.annotate_comp(c) for c in pool if isinstance(c, dict)]
    rows = []          # (confidence, make_model, actual, low, high, mid)
    skipped = 0
    for target in annotated:
        actual = target.get("price")
        ts = target.get("sold_ts")
        if not isinstance(actual, (int, float)) or actual <= 0 or not isinstance(ts, (int, float)):
            skipped += 1
            continue
        # only sales available BEFORE this one (no lookahead); exclude the target itself
        others = [c for c in annotated
                  if c.get("id") != target.get("id")
                  and isinstance(c.get("sold_ts"), (int, float)) and c["sold_ts"] <= ts]
        car = _comp_to_car(target)
        car["value"] = value.compute_value(car, others, now=ts)
        est = opportunity.estimate_range(car, others, now=ts)
        if est.get("low") is None:
            skipped += 1
            continue
        mid = (est["low"] + est["high"]) / 2
        mm = f"{car['vehicle_identity']['canonical_make']}/{car['vehicle_identity']['canonical_model']}"
        rows.append((est["confidence"], mm, float(actual), est["low"], est["high"], mid))

    return _summarize(rows, skipped, min_examples=min_examples)


def _bucket_stats(rows):
    if not rows:
        return {"n": 0, "inside_pct": None, "mid_err_median": None, "mid_err_mean": None}
    inside = sum(1 for _, _, a, lo, hi, _ in rows if lo <= a <= hi)
    errs = [abs(a - mid) / a for _, _, a, _, _, mid in rows if a]
    return {
        "n": len(rows),
        "inside_pct": round(100 * inside / len(rows), 1),
        "mid_err_median": round(100 * statistics.median(errs), 1) if errs else None,
        "mid_err_mean": round(100 * statistics.fmean(errs), 1) if errs else None,
    }


def _summarize(rows, skipped, *, min_examples):
    overall = _bucket_stats(rows)
    by_conf = {}
    for conf in ("high", "medium", "low"):
        by_conf[conf] = _bucket_stats([r for r in rows if r[0] == conf])
    by_mm = {}
    groups = {}
    for r in rows:
        groups.setdefault(r[1], []).append(r)
    for mm, rs in groups.items():
        if len(rs) >= min_examples:
            by_mm[mm] = _bucket_stats(rs)
    return {"evaluated": len(rows), "skipped_no_trusted_estimate": skipped,
            "overall": overall, "by_confidence": by_conf, "by_make_model": by_mm,
            "min_examples": min_examples}


def _fmt(rep):
    out = []
    o = rep["overall"]
    out.append("Estimate backtest (sold comps as leave-one-out, no lookahead)")
    out.append("=" * 56)
    out.append(f"Evaluated: {rep['evaluated']}   ·   skipped (no trusted estimate): {rep['skipped_no_trusted_estimate']}")
    if o["n"]:
        out.append(f"Inside predicted range: {o['inside_pct']}%")
        out.append(f"Midpoint error: median {o['mid_err_median']}%  ·  mean {o['mid_err_mean']}%")
    out.append("\nBy confidence:")
    for conf in ("high", "medium", "low"):
        b = rep["by_confidence"][conf]
        if b["n"]:
            out.append(f"  {conf:<6} n={b['n']:<4} inside={b['inside_pct']}%  mid-err median={b['mid_err_median']}%")
        else:
            out.append(f"  {conf:<6} n=0")
    out.append(f"\nBy make/model (>= {rep['min_examples']} examples): {len(rep['by_make_model'])} group(s)")
    worst = sorted(rep["by_make_model"].items(), key=lambda kv: (kv[1]["inside_pct"] if kv[1]["inside_pct"] is not None else 999))
    for mm, b in worst[:20]:
        out.append(f"  {mm:<24} n={b['n']:<4} inside={b['inside_pct']}%  mid-err median={b['mid_err_median']}%")
    return "\n".join(out)


def main(argv=None) -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = argparse.ArgumentParser(description="Backtest Stage 6B estimate ranges vs real sold prices.")
    p.add_argument("--comps", default=os.path.join(root, "data", "comps.json"))
    p.add_argument("--min-examples", type=int, default=8)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    pool = comps_mod.load_comps(args.comps)
    if not pool:
        print(f"No comps found at {args.comps} (need sold comps to backtest).", file=sys.stderr)
        return 2
    rep = evaluate(pool, min_examples=max(1, args.min_examples))
    print(json.dumps(rep, indent=2, ensure_ascii=False) if args.json else _fmt(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
