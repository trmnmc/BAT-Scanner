"""Fair value, deal detection, and appreciation from the comp pool.

Per live car, find comparable SOLD cars and summarize:
  fair_value      median sold price of the comps used
  n_comps / basis how many comps and how they were matched (precision vs breadth)
  deal_pct        (fair_value - current_bid) / fair_value   (positive = under comps)
  is_deal         deal_pct >= margin AND ending soon AND enough comps
  appreciation_pct trend of recent vs older comp medians (None until enough history)

The is_deal guard is deliberate: a live auction's current bid is low early and ramps at
the close, so "under comps" only means a likely deal when the auction is about to END.
Everything is None/false when comps are too thin to trust.
"""

from __future__ import annotations

import datetime as _dt

from . import identity

YEAR_BAND = 3                 # tier 1: same make+model within +/- this many years
YEAR_BAND_WIDE = 7            # tier 2/3: widen the year window
MIN_COMPS = 5                 # need at least this many comps to trust a fair value
DEAL_MARGIN = 0.15            # >= 15% under comp median to call it a deal
ENDING_SOON_SECONDS = 48 * 3600
APPR_RECENT_SECONDS = 180 * 86400      # "recent" = last 180 days
APPR_OLDER_SECONDS = 540 * 86400       # "older" = 180-540 days ago
APPR_MIN_PER_BUCKET = 3

# Phase 4: mileage/age/condition tilt on the deal score. deal_pct stays the pure comp
# signal; the tilt nudges deal_score so a clean low-mileage car ranks above a worn or
# modified one at the same price. It NEVER changes fair_value and NEVER flips is_deal.
TILT_WEIGHT = 1.0            # global multiplier; 90% mileage hit-rate (Phase 3) justifies 1.0
TILT_CLAMP = 0.12            # deal_score = deal_pct + clamp(tilt, +/-TILT_CLAMP)
MPY_BASELINE = 7500          # "average" miles/year; below = nicer, above = worn
MILEAGE_TILT_MAX = 0.06
COND_TILT = {               # per-flag nudge; a missing flag is always 0, never a penalty
    "numbers-matching": +0.02, "original-paint": +0.02,
    "repaint": -0.02, "rebuilt-engine": -0.02,
    "modified": -0.03, "restomod": -0.03, "engine-swap": -0.03,
    "replica": -0.03, "tribute": -0.03, "kit-car": -0.03,
    "project": -0.03, "salvage-title": -0.03,
}
COND_GOOD_CAP = 0.06
COND_BAD_CAP = -0.08


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _select_comps(car, comps):
    """Tiered comp selection on CANONICAL make+model: prefer a tight year band, widen if too few.

    Matching prefers each record's canonical identity (so "El Camino" comps against "El Camino",
    never the broken fragment "El"), with a graceful fall-back to legacy slugs for records that
    carry no identity. Returns (list_of_comps, basis_string).
    """
    cmake, cmodel = identity.car_canonical(car)
    year = car.get("year")
    if not isinstance(year, int):
        return [], "no-year"
    if not (cmake and cmodel):
        return [], "no-identity"

    tier1, tier2 = [], []
    for c in comps:
        ccm, ccmod = identity.comp_canonical(c)
        if ccm != cmake or ccmod != cmodel:
            continue                                   # different car — never a category blend
        cy = c.get("year")
        if not isinstance(cy, int):
            continue
        dy = abs(cy - year)
        if dy <= YEAR_BAND:
            tier1.append(c)
            tier2.append(c)
        elif dy <= YEAR_BAND_WIDE:
            tier2.append(c)

    if len(tier1) >= MIN_COMPS:
        return tier1, "make-model-y3"
    if len(tier2) >= MIN_COMPS:
        return tier2, "make-model-y7"
    # Not enough same-model comps. We deliberately do NOT fall back to a whole-category
    # median: it blends, say, a $250k 911 RS with a $40k 912 and lies about fair value.
    # Show the few same-model comps we have, flagged insufficient, and never call it a deal.
    return tier2, "insufficient"


def select_comps(car, comps):
    """Public access to the tiered comp selection (the SAME engine valuation uses), so opportunity.py
    can build an estimate band + liquidity signal from the very comps that set fair_value — without a
    second matching implementation. Returns (selected_comps, basis_string)."""
    return _select_comps(car, comps)


def _mileage_tilt(details, year, now_year):
    """+ for below-average miles/year, - for above; 0 if TMU/missing/no year."""
    if not details or details.get("tmu"):
        return 0.0
    miles = details.get("miles")
    if miles is None or not isinstance(year, int) or not isinstance(now_year, int):
        return 0.0
    age = max(1, now_year - year)
    mpy = miles / age
    t = (MPY_BASELINE - mpy) / MPY_BASELINE * MILEAGE_TILT_MAX
    return max(-MILEAGE_TILT_MAX, min(MILEAGE_TILT_MAX, t))


def _condition_tilt(details):
    """Sum of per-flag nudges, good and bad capped independently. Missing -> 0."""
    if not details:
        return 0.0
    good = bad = 0.0
    for f in details.get("condition") or []:
        v = COND_TILT.get(f, 0.0)
        if v > 0:
            good += v
        elif v < 0:
            bad += v
    return min(good, COND_GOOD_CAP) + max(bad, COND_BAD_CAP)


def _deal_tilt(details, year, now_year):
    t = (_mileage_tilt(details, year, now_year) + _condition_tilt(details)) * TILT_WEIGHT
    return max(-TILT_CLAMP, min(TILT_CLAMP, t))


def _suppressed_value(reasons):
    """A trusted valuation is SUPPRESSED for a low-confidence identity: no fair value, no deal.
    The basis is its own non-trusted string so the frontend (which trusts only the y3/y7 bases)
    never shows a confident price for a car we can't reliably identify."""
    return {
        "fair_value": None, "n_comps": 0, "basis": "low-confidence-identity",
        "deal_pct": None, "is_deal": False, "tilt": None, "deal_score": None,
        "appreciation_pct": None, "identity_confidence": "low", "match_reasons": reasons,
    }


def compute_value(car, comps, *, now: float):
    # Stage 6A: a low-confidence vehicle_identity (a chopped multiword model, a bare Mercedes
    # number, an unrecognized make+model) must NOT receive a trusted valuation — suppress it.
    vi = car.get("vehicle_identity")
    if identity.is_low_confidence(vi):
        reasons = [r for r in ((vi or {}).get("ambiguity_reasons") or [])] or ["identity confidence low"]
        return _suppressed_value(["valuation suppressed (" + r + ")" for r in reasons])

    selected, basis = _select_comps(car, comps)
    n = len(selected)
    prices = [c["price"] for c in selected if c.get("price")]
    fair = _median(prices)
    bid = (car.get("bid") or {}).get("amount")
    enough = n >= MIN_COMPS and basis != "insufficient"

    deal_pct = None
    if fair and bid:
        deal_pct = round((fair - bid) / fair, 4)

    ends = car.get("_ends_ts")
    ending_soon = isinstance(ends, (int, float)) and 0 <= (ends - now) <= ENDING_SOON_SECONDS
    # A flagged DEAL requires NO RESERVE: on a reserve auction the current bid is not the
    # price (it may be far below an unmet reserve and will jump or not sell), so "under comps"
    # is meaningless. On a no-reserve car the current bid IS the price, so under-comps +
    # ending-soon is a genuine likely-steal.
    no_reserve = bool((car.get("flags") or {}).get("no_reserve"))
    is_deal = bool(enough and deal_pct is not None and deal_pct >= DEAL_MARGIN
                   and ending_soon and no_reserve)

    appreciation_pct = None
    if enough:
        recent = [c["price"] for c in selected
                  if c.get("sold_ts") and (now - c["sold_ts"]) <= APPR_RECENT_SECONDS]
        older = [c["price"] for c in selected
                 if c.get("sold_ts") and APPR_RECENT_SECONDS < (now - c["sold_ts"]) <= APPR_OLDER_SECONDS]
        if len(recent) >= APPR_MIN_PER_BUCKET and len(older) >= APPR_MIN_PER_BUCKET:
            mr, mo = _median(recent), _median(older)
            if mr and mo:
                appreciation_pct = round((mr - mo) / mo, 4)

    # deal_score = deal_pct nudged by mileage/condition. Only when scoreable, so a
    # thin-comp car never gets a score (the frontend shows no green/deal for it).
    tilt = deal_score = None
    if enough and deal_pct is not None:
        now_year = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc).year
        tilt = round(_deal_tilt(car.get("details"), car.get("year"), now_year), 4)
        deal_score = round(deal_pct + tilt, 4)

    # match_reasons: ALWAYS explain how (or why not) comps were matched — and make the
    # never-blend-a-category-median rule explicit when comps are too thin (Stage 6A tasks 15/16).
    cmake, cmodel = identity.car_canonical(car)
    if basis in ("make-model-y3", "make-model-y7"):
        band = YEAR_BAND if basis == "make-model-y3" else YEAR_BAND_WIDE
        match_reasons = [f"{n} comps matched on canonical make+model {cmake}/{cmodel} within ±{band}y"]
    elif basis == "insufficient":
        match_reasons = [f"only {n} same-model ({cmake}/{cmodel}) comps (< {MIN_COMPS}); "
                         f"not enough to trust — never blended with a broad-category median"]
    elif basis == "no-year":
        match_reasons = ["no usable year on the listing"]
    elif basis == "no-identity":
        match_reasons = ["no canonical make/model could be resolved from the title"]
    else:
        match_reasons = []

    return {
        "fair_value": int(fair) if fair else None,
        "n_comps": n,
        "basis": basis,
        "deal_pct": deal_pct,
        "is_deal": is_deal,
        "tilt": tilt,
        "deal_score": deal_score,
        "appreciation_pct": appreciation_pct,
        "identity_confidence": (vi or {}).get("confidence"),
        "match_reasons": match_reasons,
    }
