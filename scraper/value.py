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

YEAR_BAND = 3                 # tier 1: same make+model within +/- this many years
YEAR_BAND_WIDE = 7            # tier 2/3: widen the year window
MIN_COMPS = 5                 # need at least this many comps to trust a fair value
DEAL_MARGIN = 0.15            # >= 15% under comp median to call it a deal
ENDING_SOON_SECONDS = 48 * 3600
APPR_RECENT_SECONDS = 180 * 86400      # "recent" = last 180 days
APPR_OLDER_SECONDS = 540 * 86400       # "older" = 180-540 days ago
APPR_MIN_PER_BUCKET = 3


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _select_comps(car, comps):
    """Tiered comp selection: prefer same make+model+tight year, widen if too few.
    Returns (list_of_comps, basis_string)."""
    make = (car.get("make") or {}).get("slug")
    model = (car["models"][0]["slug"] if car.get("models") else None)
    year = car.get("year")
    if not isinstance(year, int):
        return [], "no-year"

    def mm(c, band):
        return (c.get("make") == make and c.get("model") == model and make and model
                and isinstance(c.get("year"), int) and abs(c["year"] - year) <= band)

    tier1 = [c for c in comps if mm(c, YEAR_BAND)]
    if len(tier1) >= MIN_COMPS:
        return tier1, "make-model-y3"
    tier2 = [c for c in comps if mm(c, YEAR_BAND_WIDE)]
    if len(tier2) >= MIN_COMPS:
        return tier2, "make-model-y7"
    # Not enough same-model comps. We deliberately do NOT fall back to a whole-category
    # median: it blends, say, a $250k 911 RS with a $40k 912 and lies about fair value.
    # Show the few same-model comps we have, flagged insufficient, and never call it a deal.
    return tier2, "insufficient"


def compute_value(car, comps, *, now: float):
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
    is_deal = bool(enough and deal_pct is not None and deal_pct >= DEAL_MARGIN and ending_soon)

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

    return {
        "fair_value": int(fair) if fair else None,
        "n_comps": n,
        "basis": basis,
        "deal_pct": deal_pct,
        "is_deal": is_deal,
        "appreciation_pct": appreciation_pct,
    }
