#!/usr/bin/env python3
"""Identity report — a read-only audit of canonical vehicle identity across a snapshot.

    python tools/identity_report.py [--snapshot data/auctions.json]
                                    [--overrides data/identity_overrides.json] [--json] [--limit N]

Surfaces (Stage 6A task 17):
  1. low-confidence identities          — cars whose identity is too shaky to value
  2. model collisions                   — distinct title-models that share one canonical model
  3. manual overrides                   — every override key, and whether it hit a live car
  4. live cars with no reliable comp identity — low confidence OR a non-trusted value basis

Read-only and offline: it never fetches anything and never writes the snapshot. It uses each
car's stored vehicle_identity when present, and otherwise derives it on the fly, so it works on
older snapshots too.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import identity  # noqa: E402

TRUSTED_BASES = ("make-model-y3", "make-model-y7")


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _identity_of(car, overrides):
    vi = car.get("vehicle_identity")
    if isinstance(vi, dict) and vi.get("confidence"):
        return vi
    return identity.derive_identity(car, overrides)


def build_report(snapshot, overrides, *, limit=40):
    cars = (snapshot or {}).get("auctions") or []
    rows = []
    for c in cars:
        vi = _identity_of(c, overrides)
        rows.append((c, vi))

    # 1. low-confidence identities
    low = [(c, vi) for c, vi in rows if identity.is_low_confidence(vi)]

    # 2. model collisions: distinct raw title-model phrases that collapsed to the same
    #    (canonical_make, canonical_model). A benign multi-trim bucket under an AUTHORITATIVE model
    #    (a 911's trims, an el-camino) is NOT a collision — only a NON-authoritative canonical
    #    (a single-token fallback / chopped model / registry gap) reached from 2+ distinct
    #    title-models is flagged, so the signal isn't drowned by every trim variant.
    groups: dict = {}
    for c, vi in rows:
        key = (vi.get("canonical_make"), vi.get("canonical_model"))
        if not key[1]:
            continue
        phrase = " ".join(identity.model_phrase(c.get("title", ""), (c.get("make") or {}).get("name"))[:3]).lower()
        groups.setdefault(key, {})
        groups[key].setdefault(phrase, []).append(c.get("title", ""))
    collisions = {(mk, mdl): v for (mk, mdl), v in groups.items()
                  if len([p for p in v if p]) > 1 and not identity.is_known_model(mk, mdl)}

    # 3. manual overrides: which keys exist, and which matched a live car this snapshot
    applied = set()
    for c, vi in rows:
        if vi.get("manually_overridden"):
            rid = c.get("id")
            applied.add(f"bat:{rid}" if rid is not None else c.get("listing_url"))
    override_rows = []
    for key, fields in (overrides or {}).items():
        override_rows.append({"key": key, "fields": fields, "applied_in_snapshot": key in applied})

    # 4. live cars with no reliable comp IDENTITY — an identity problem, NOT a thin comp pool: the
    #    identity is low-confidence, has no canonical model, or has no year to band on. Cars that
    #    simply lack enough comps (basis "insufficient") but have a GOOD identity are excluded — the
    #    pool just needs to accumulate.
    unreliable = []
    for c, vi in rows:
        year = c.get("year")
        if (identity.is_low_confidence(vi) or not vi.get("canonical_model")
                or not isinstance(year, int)):
            unreliable.append((c, vi, (c.get("value") or {}).get("basis")))

    return {
        "total": len(cars),
        "low_confidence": low,
        "collisions": collisions,
        "overrides": override_rows,
        "unreliable": unreliable,
        "limit": limit,
    }


def _fmt_text(rep) -> str:
    out = []
    lim = rep["limit"]
    out.append(f"Identity report — {rep['total']} live car(s)\n" + "=" * 48)

    out.append(f"\n1. LOW-CONFIDENCE IDENTITIES (valuation suppressed): {len(rep['low_confidence'])}")
    for c, vi in rep["low_confidence"][:lim]:
        reasons = "; ".join(vi.get("ambiguity_reasons") or []) or "—"
        out.append(f"   • [{c.get('id')}] {c.get('title', '')[:60]}")
        out.append(f"        make/model={vi.get('canonical_make')}/{vi.get('canonical_model')}  reasons: {reasons}")

    out.append(f"\n2. MODEL COLLISIONS (distinct title-models sharing one non-authoritative canonical): "
               f"{len(rep['collisions'])}")
    worst = sorted(rep["collisions"].items(), key=lambda kv: -len(kv[1]))   # most-collided first
    for (mk, mdl), phrases in worst[:lim]:
        out.append(f"   • {mk}/{mdl} ← {len(phrases)} distinct title-models:")
        for phrase, titles in phrases.items():
            out.append(f"        '{phrase}'  ×{len(titles)}  e.g. {titles[0][:50]}")

    out.append(f"\n3. MANUAL OVERRIDES: {len(rep['overrides'])}")
    for o in rep["overrides"][:lim]:
        hit = "applied" if o["applied_in_snapshot"] else "no live car this snapshot"
        out.append(f"   • {o['key']}  ({hit})  -> {json.dumps(o['fields'])}")
    if not rep["overrides"]:
        out.append("   (none configured — the registry handles systematic cases)")

    out.append(f"\n4. LIVE CARS WITH NO RELIABLE COMP IDENTITY: {len(rep['unreliable'])}")
    for c, vi, basis in rep["unreliable"][:lim]:
        out.append(f"   • [{c.get('id')}] {c.get('title', '')[:55]}  "
                   f"conf={vi.get('confidence')} basis={basis}")
    if len(rep["unreliable"]) > lim:
        out.append(f"   … and {len(rep['unreliable']) - lim} more (raise --limit to see them)")
    return "\n".join(out)


def _to_jsonable(rep):
    def car_brief(c, vi, basis=None):
        d = {"id": c.get("id"), "title": c.get("title"),
             "canonical_make": vi.get("canonical_make"), "canonical_model": vi.get("canonical_model"),
             "confidence": vi.get("confidence"), "ambiguity_reasons": vi.get("ambiguity_reasons")}
        if basis is not None:
            d["value_basis"] = basis
        return d
    return {
        "total": rep["total"],
        "low_confidence": [car_brief(c, vi) for c, vi in rep["low_confidence"]],
        "collisions": [{"canonical": f"{mk}/{mdl}", "title_models": phrases}
                       for (mk, mdl), phrases in rep["collisions"].items()],
        "overrides": rep["overrides"],
        "unreliable": [car_brief(c, vi, basis) for c, vi, basis in rep["unreliable"]],
    }


def main(argv=None) -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = argparse.ArgumentParser(description="Canonical vehicle-identity audit (read-only).")
    p.add_argument("--snapshot", default=os.path.join(root, "data", "auctions.json"))
    p.add_argument("--overrides", default=os.path.join(root, "data", "identity_overrides.json"))
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p.add_argument("--limit", type=int, default=40, help="max rows printed per section")
    args = p.parse_args(argv)
    args.limit = max(0, args.limit)        # a negative limit would miscount the per-section overflow

    snapshot = _load_json(args.snapshot)
    if snapshot is None:
        print(f"Could not read snapshot: {args.snapshot}", file=sys.stderr)
        return 2
    overrides = identity.load_overrides(args.overrides)
    rep = build_report(snapshot, overrides, limit=args.limit)
    if args.json:
        print(json.dumps(_to_jsonable(rep), indent=2, ensure_ascii=False))
    else:
        print(_fmt_text(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
