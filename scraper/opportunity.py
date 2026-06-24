"""scraper/opportunity.py — cautious, explainable opportunity scoring + production badges.

Builds ON TOP of value.py's comp-derived `value` block (it never recomputes or overwrites it).
Produces three additive things per live car, plus a board-wide badge pass:

  - estimate: a cautious final-price RANGE {low, high, currency, confidence, model_version,
    comp_ids, adjustment_reasons, reserve_uncertainty} — always a BAND, never a point, and never
    below the current bid. A trusted range needs a trusted comp basis AND a confident, unambiguous
    identity; otherwise low/high are None and the status is "too_early_to_estimate".

  - opportunity: a MARKET-ONLY Opportunity Score (0-100 | None) from four weighted components, each
    fully explainable (score / confidence / coverage / machine reasons / readable reasons / missing
    inputs), plus a tracking status. Personal data (watchlists, dad's favorites, notes, budgets) is
    NEVER an input — this is about the market, not about a particular user.

  - badges: scarce production badges (Diamond/Flame/Trophy/Warning) chosen board-wide with BOTH an
    absolute score floor AND a board-percentile threshold, capped as a share of the live board, with
    at most one MAIN badge per auction. Radar stays watchlist-driven and Ghost stays historical
    (both handled elsewhere) — this module emits only opportunity/hot/trophy/warning.

Invariants: no AI, no network, no automated bidding. Missing information LOWERS confidence, never
zeroes a score. Watchers/comments influence INTEREST only — never the value range. Reserve auctions
keep a separate reserve-uncertainty note, and a low early bid never auto-becomes an opportunity.
"""

from __future__ import annotations

import bisect
import statistics

MODEL_VERSION = "6b.1"

# (1) ONE obvious configuration object for the component weights. (2) Validated to total 1.0.
OPPORTUNITY_WEIGHTS = {
    "investment_quality": 0.40,
    "enthusiast_appeal": 0.25,
    "below_market_chance": 0.20,
    "auction_interestingness": 0.15,
}


def _validate_weights(weights):
    total = round(sum(weights.values()), 6)
    if total != 1.0:
        raise ValueError(f"OPPORTUNITY_WEIGHTS must total 1.0; got {total} ({weights})")


_validate_weights(OPPORTUNITY_WEIGHTS)

TRUSTED_BASES = {"make-model-y3", "make-model-y7"}
CONF_HIGH, CONF_MEDIUM, CONF_LOW, CONF_NONE = "high", "medium", "low", "none"
_CONF_RANK = {CONF_NONE: 0, CONF_LOW: 1, CONF_MEDIUM: 2, CONF_HIGH: 3}

HOUR = 3600
NEAR_CLOSE_WINDOW = 72 * HOUR        # a below-estimate bid only "counts" as it nears the close
ENTHUSIAST_TRANSMISSIONS = {"manual"}
DESIRABLE_BODY = {"coupe", "convertible", "targa", "wagon"}
DESIRABLE_ENGINE = {"v12", "v10", "v8", "flat-6", "inline-6"}
RISK_CONDITION_FLAGS = {"salvage-title", "project", "engine-swap", "replica", "tribute", "kit-car"}
GOOD_CONDITION_FLAGS = {"numbers-matching", "original-paint"}

# Tracking statuses (machine codes) + the only allowed human phrases (cautious language).
TRACK_TOO_EARLY = "too_early_to_estimate"
TRACK_BELOW = "trading_below_expected"
TRACK_NEAR = "tracking_near_expected"
TRACK_ABOVE = "tracking_above_expected"

APPROVED_PHRASES = {
    TRACK_BELOW: "Trading below expected range",
    TRACK_NEAR: "Tracking near expected range",
    TRACK_ABOVE: "Tracking above expected range",
    TRACK_TOO_EARLY: "Too early to estimate",
    "low_confidence": "Low confidence estimate",
    "high_interest": "High-interest auction",
    "potential": "Potential opportunity",
    "watch": "Watch closely",
}

# Board-wide badge selection: each MAIN badge needs an absolute floor AND a board percentile; the
# total share of the board carrying a main badge is capped; at most one main badge per auction.
BADGE_RULES = {
    "opportunity": {"min_score": 66, "percentile": 0.90},   # Diamond — a strong, confident opportunity
    "trophy":      {"min_score": 70, "percentile": 0.90},   # Trophy — strong investment + enthusiast appeal
    "hot":         {"min_score": 72, "percentile": 0.92},   # Flame  — unusually interesting auction
}
MAIN_BADGE_PRIORITY = ["opportunity", "trophy", "hot"]      # if a car qualifies for several, keep this one
MAIN_BADGE_CAP_FRACTION = 0.12                              # at most ~12% of the live board gets a main badge
DIAMOND_MIN_BELOW_MARKET = 55                              # Diamond also needs a real below-market chance
TROPHY_MIN_EACH = 62                                       # Trophy needs BOTH pillars individually strong


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _eng(car, key):
    e = car.get("engagement") or {}
    return _num(e.get(key))


def _identity(car):
    return car.get("vehicle_identity") or {}


def _identity_confident(car):
    """Identity good enough to anchor a trusted valuation: high/medium, never low/None (Stage 6A)."""
    return _identity(car).get("confidence") in (CONF_HIGH, CONF_MEDIUM)


def _value(car):
    return car.get("value") or {}


def _trusted_value(car):
    v = _value(car)
    return bool(v.get("basis") in TRUSTED_BASES and _num(v.get("fair_value"))
                and v["fair_value"] > 0 and _identity_confident(car))


def _no_reserve(car):
    return bool((car.get("flags") or {}).get("no_reserve"))


def _hours_left(car, now):
    ends = _num(car.get("_ends_ts"))
    if ends is None:
        return None
    return (ends - now) / HOUR


def _percentile_rank(sorted_vals, x):
    """Fraction of board values <= x, in [0,1]; None if there is no distribution."""
    if not sorted_vals:
        return None
    return bisect.bisect_right(sorted_vals, x) / len(sorted_vals)


def _component(score, confidence, coverage, reasons, readable, missing):
    return {
        "score": score, "confidence": confidence, "coverage": round(coverage, 3),
        "reasons": reasons, "readable": readable, "missing": missing,
    }


def _coverage_confidence(coverage, *, ceiling=CONF_HIGH):
    """Missing inputs LOWER confidence but never zero it: even sparse data yields at least low."""
    if coverage >= 0.6:
        conf = CONF_HIGH
    elif coverage >= 0.35:
        conf = CONF_MEDIUM
    else:
        conf = CONF_LOW
    return conf if _CONF_RANK[conf] <= _CONF_RANK[ceiling] else ceiling


# ---------------------------------------------------------------------------
# estimate range (comp-based; watchers/comments NEVER feed this)
# ---------------------------------------------------------------------------
def estimate_range(car, comps, *, now, select=None):
    """A cautious final-price BAND from the trusted comps. Returns the estimate block. No trusted
    range (low/high None) unless the comp basis is trusted AND the identity is confident+unambiguous;
    the band is clamped to never sit below the current bid; reserve auctions carry a reserve note."""
    cur = (car.get("bid") or {}).get("currency")
    bid = _num((car.get("bid") or {}).get("amount"))
    reserve = not _no_reserve(car)

    def _empty(reasons):
        return {"low": None, "high": None, "currency": cur, "confidence": CONF_NONE,
                "model_version": MODEL_VERSION, "comp_ids": [], "adjustment_reasons": reasons,
                "reserve_uncertainty": _reserve_block(reserve, None, None)}

    if not _trusted_value(car):
        vi = _identity(car)
        if vi.get("confidence") == CONF_LOW:
            return _empty(["identity low-confidence — no trusted estimate"])
        return _empty(["no trusted comp basis (insufficient/ambiguous) — no trusted estimate"])

    selected, basis = (select or _default_select)(car, comps)
    prices = sorted(p for p in (_num(c.get("price")) for c in selected) if p and p > 0)
    # only list comps that actually BACKED the band (a priced comp); an unpriced one didn't contribute.
    comp_ids = [c.get("id") for c in selected
                if c.get("id") is not None and _num(c.get("price")) and c["price"] > 0]
    if len(prices) < 3:
        return _empty([f"only {len(prices)} usable comp prices — no trusted estimate"])

    # An interDECILE band (p10-p90), not a tight interquartile one: a model like "911" spans base to
    # GT3, so a narrow band is false precision. A wide band + honest (spread-driven) confidence is the
    # cautious, accurate choice — the band's WIDTH already tells the reader how uncertain this is.
    lo_raw = _quantile(prices, 0.10)
    hi_raw = _quantile(prices, 0.90)
    reasons = [f"interdecile comp band p10-p90 of {len(prices)} {basis} comps"]

    # apply the deterministic mileage/condition tilt the value block already computed (a clean low-
    # mileage car shifts the band up; a worn/modified one shifts it down). Bounded, and explained.
    tilt = _num(_value(car).get("tilt"))
    if tilt:
        lo_raw *= (1 + tilt)
        hi_raw *= (1 + tilt)
        reasons.append(f"mileage/condition tilt {tilt:+.2%}")
    appr = _num(_value(car).get("appreciation_pct"))
    if appr is not None:
        reasons.append(f"recent comp trend {appr:+.1%}")

    # Floor the band to +/-12% of the comp median: even a tight comp cluster can't pin one specific
    # car's trim/options/condition, so an ultra-narrow band would be false precision. The floor is
    # centered on the TILTED median so the already-applied mileage/condition tilt is preserved.
    median = _quantile(prices, 0.5)
    center = median * (1 + tilt) if (median and tilt) else median
    if center and (hi_raw - lo_raw) < 0.24 * center:
        lo_raw, hi_raw = center * 0.88, center * 1.12
        reasons.append("band floored to +/-12% of the comp median (irreducible per-car variance)")

    lo = int(round(min(lo_raw, hi_raw)))
    hi = int(round(max(lo_raw, hi_raw)))
    # (13) the range can NEVER be below the current bid: a final price won't drop under the standing
    # bid. But the band must never collapse to a POINT either — when the bid is at/above the comp band,
    # keep a genuine band ABOVE the bid (and reduce confidence), rather than emitting a false-precise
    # zero-width estimate.
    widened_off_bid = False
    if bid is not None:
        b = int(round(bid))
        if lo < b:
            lo = b
        if hi <= lo:
            hi = int(round(lo * 1.12))
            widened_off_bid = True
            reasons.append("current bid at/above comps — band re-floored above the bid (not a point estimate)")
    if hi < lo:
        hi = lo

    conf = _estimate_confidence(basis, len(prices), prices, car)
    if widened_off_bid:
        conf = _down_one(conf)     # a band pinned above the bid is less certain than a comp-anchored one
    if reserve:
        conf = _down_one(conf)
        reasons.append("reserve auction — confidence reduced (final depends on an unknown reserve)")
    return {
        "low": lo, "high": hi, "currency": cur, "confidence": conf, "model_version": MODEL_VERSION,
        "comp_ids": comp_ids, "adjustment_reasons": reasons,
        "reserve_uncertainty": _reserve_block(reserve, lo, hi),
    }


def _reserve_block(reserve, lo, hi):
    if not reserve:
        return None
    return {"reserve": True,
            "note": "Current bid may be below an unmet reserve; the final price is uncertain until "
                    "the reserve is met (or the car goes unsold).",
            "band_is_conditional": lo is not None}


def _estimate_confidence(basis, n, prices, car):
    # Confidence is driven primarily by the comp SPREAD (coefficient of variation): a tight cluster of
    # comps is a reliable estimate; a wide one (a heterogeneous model lumped together) is not — no
    # matter how many comps or how tight the year band. Then nudged down for a wider basis / thin pool
    # / a merely-medium identity. This makes a higher stated confidence genuinely more accurate.
    try:
        mean = statistics.fmean(prices)
        cv = statistics.pstdev(prices) / mean if mean > 0 else 1.0
    except statistics.StatisticsError:
        cv = 1.0
    base = CONF_HIGH if cv < 0.20 else (CONF_MEDIUM if cv < 0.40 else CONF_LOW)
    if basis != "make-model-y3" or n < 5:
        base = _down_one(base)
    if _identity(car).get("confidence") == CONF_MEDIUM:
        base = _down_one(base)
    return base


def _down_one(conf):
    return {CONF_HIGH: CONF_MEDIUM, CONF_MEDIUM: CONF_LOW, CONF_LOW: CONF_LOW, CONF_NONE: CONF_NONE}[conf]


def _quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(sorted_vals):
        return sorted_vals[-1]
    return sorted_vals[lo] * (1 - frac) + sorted_vals[lo + 1] * frac


def _default_select(car, comps):
    # imported lazily to avoid a hard import cycle; value.py is the single comp-matching engine.
    from . import value
    return value.select_comps(car, comps)


# ---------------------------------------------------------------------------
# components — each returns the full explainable contract (req 3)
# ---------------------------------------------------------------------------
def investment_quality(car, comps, *, select=None):
    v = _value(car)
    reasons, readable, missing = [], [], []
    pts, have, total = [], 0, 6

    appr = _num(v.get("appreciation_pct"))
    if appr is not None:
        c = _clamp(appr * 150, -15, 15)
        pts.append(c); have += 1
        reasons.append({"code": "comp_trend", "appreciation_pct": appr})
        readable.append(("Comps trending up " if appr >= 0 else "Comps trending down ") + f"{abs(appr):.0%}")
    else:
        missing.append("comp price trend (need more dated comps)")

    selected, basis = (select or _default_select)(car, comps)
    prices = [p for p in (_num(c.get("price")) for c in selected) if p and p > 0]
    if len(prices) >= 5:
        mean = statistics.fmean(prices)
        cv = statistics.pstdev(prices) / mean if mean else 1.0
        c = _clamp((0.4 - cv) / 0.4 * 12, -12, 12)
        pts.append(c); have += 1
        reasons.append({"code": "price_consistency", "cv": round(cv, 3)})
        readable.append("Tight, consistent comp prices" if cv < 0.25 else
                        ("Moderate comp price spread" if cv < 0.45 else "Wide comp price spread"))
        # liquidity
        c2 = _clamp((len(prices) - 5) / 15 * 12, -4, 12)
        pts.append(c2); have += 1
        reasons.append({"code": "liquidity", "n_comps": len(prices)})
        readable.append(f"{len(prices)} comparable sales (liquid market)" if len(prices) >= 10
                        else f"{len(prices)} comparable sales")
    else:
        missing.append("price consistency / liquidity (too few comps)")
        missing.append("market liquidity (too few comps)")

    vi = _identity(car)
    orig = vi.get("originality")
    if orig is not None:
        c = {"original": 10, "restored": 4, "modified": -8, "replica": -14}.get(orig, 0)
        pts.append(c); have += 1
        reasons.append({"code": "originality", "originality": orig})
        readable.append(f"Originality: {orig}")
    else:
        missing.append("originality (not stated in title)")

    conf = vi.get("confidence")
    if conf:
        c = {CONF_HIGH: 6, CONF_MEDIUM: 0, CONF_LOW: -12}.get(conf, 0)
        pts.append(c); have += 1
        reasons.append({"code": "identity_confidence", "confidence": conf})
        readable.append(f"Identity confidence: {conf}")

    cond = (car.get("details") or {}).get("condition") or []
    bad = [f for f in cond if f in RISK_CONDITION_FLAGS]
    good = [f for f in cond if f in GOOD_CONDITION_FLAGS]
    if cond or (car.get("details") is not None):
        c = len(good) * 4 - len(bad) * 8
        pts.append(_clamp(c, -16, 8)); have += 1
        if bad:
            reasons.append({"code": "title_condition_risk", "flags": bad})
            readable.append("Condition/title risk: " + ", ".join(bad))
        if good:
            readable.append("Positive condition: " + ", ".join(good))
    else:
        missing.append("condition/title (listing not enriched)")

    score = int(round(_clamp(50 + sum(pts), 0, 100)))
    coverage = have / total
    return _component(score, _coverage_confidence(coverage), coverage, reasons, readable, missing)


def enthusiast_appeal(car):
    vi = _identity(car)
    reasons, readable, missing = [], [], []
    pts, have, total = [], 0, 6

    cats = car.get("category_ids") or []
    if cats:
        pts.append(12); have += 1
        reasons.append({"code": "enthusiast_taxonomy", "categories": cats})
        readable.append("Enthusiast category: " + ", ".join(cats))
    else:
        # not in a taste category isn't a penalty — it's just less signal
        have += 0

    body = vi.get("body_style")
    if body is not None:
        c = 8 if body in DESIRABLE_BODY else 0
        pts.append(c); have += 1
        reasons.append({"code": "body_style", "body_style": body})
        readable.append(f"Body style: {body}")
    else:
        missing.append("body style")

    engine = vi.get("engine")
    if engine is not None:
        c = 6 if engine in DESIRABLE_ENGINE else 2
        pts.append(c); have += 1
        reasons.append({"code": "engine", "engine": engine})
        readable.append(f"Engine: {engine}")
    else:
        missing.append("engine")

    trans = vi.get("transmission")
    if trans is not None:
        c = 12 if trans in ENTHUSIAST_TRANSMISSIONS else -3
        pts.append(c); have += 1
        reasons.append({"code": "transmission", "transmission": trans})
        readable.append("Manual transmission" if trans == "manual" else f"Transmission: {trans}")
    else:
        missing.append("transmission")

    drive = vi.get("drivetrain")
    if drive is not None:
        pts.append(3 if drive in ("rwd", "awd") else 0); have += 1
        reasons.append({"code": "drivetrain", "drivetrain": drive})
        readable.append(f"Drivetrain: {drive}")
    else:
        missing.append("drivetrain")

    # rarity: a confidently-identified model with very few comps is scarce (NOT thin-data noise).
    n = _num(_value(car).get("n_comps"))
    if n is not None and _identity_confident(car):
        c = 9 if n <= 4 else (4 if n <= 8 else 0)
        pts.append(c); have += 1
        reasons.append({"code": "rarity", "n_comps": n})
        if n <= 4:
            readable.append("Rarely traded (few comparable sales)")
    else:
        missing.append("rarity (identity/comps unavailable)")

    orig = vi.get("originality")
    if orig is not None:
        pts.append({"original": 6, "restored": 3, "modified": 2, "replica": -8}.get(orig, 0))
        reasons.append({"code": "originality", "originality": orig})

    score = int(round(_clamp(45 + sum(pts), 0, 100)))
    coverage = have / total
    return _component(score, _coverage_confidence(coverage), coverage, reasons, readable, missing)


def below_market_chance(car, estimate, *, now):
    """Chance the FINAL settles below the expected range — a genuine opportunity. A low EARLY bid is
    NOT one: the signal is scaled by how close the auction is to ending, withheld on reserve lots, and
    requires a trusted estimate. (req 8, 9)"""
    reasons, readable, missing = [], [], []
    if estimate.get("low") is None:
        return _component(None, CONF_NONE, 0.0, reasons, ["No trusted estimate to compare the bid against"],
                          ["trusted estimate range"])
    bid = _num((car.get("bid") or {}).get("amount"))
    if bid is None:
        return _component(None, CONF_LOW, 0.2, reasons, ["No bid yet"], ["current bid"])

    mid = (estimate["low"] + estimate["high"]) / 2
    gap = (mid - bid) / mid if mid else 0.0           # +ve => bid sits below the expected mid
    hrs = _hours_left(car, now)
    have, total = 0, 3

    # time factor: a below-mid bid only becomes a real "below-market chance" as the close nears.
    if hrs is None:
        time_factor = 0.4
        missing.append("time remaining (no end time)")
    else:
        have += 1
        time_factor = _clamp((NEAR_CLOSE_WINDOW / HOUR - hrs) / (NEAR_CLOSE_WINDOW / HOUR), 0.0, 1.0)
        reasons.append({"code": "time_remaining_hours", "hours": round(hrs, 1)})
        if hrs > 48:
            readable.append("Still early — a low bid is expected to climb before the close")

    raw = gap * time_factor
    score = int(round(_clamp(50 + raw * 180, 0, 100)))
    have += 1
    reasons.append({"code": "bid_vs_estimate_mid", "gap_pct": round(gap, 4)})
    readable.append((f"Bid ~{gap:.0%} below the expected mid" if gap > 0 else
                     f"Bid ~{abs(gap):.0%} above the expected mid"))

    reserve = not _no_reserve(car)
    if reserve:
        score = int(round(score * 0.5))
        reasons.append({"code": "reserve", "reserve": True})
        readable.append("Reserve auction — the bid may be below reserve, so 'below market' is uncertain")
    else:
        have += 1
        reasons.append({"code": "reserve", "reserve": False})

    # confidence inherits the estimate's confidence (comp confidence), floored by coverage.
    coverage = have / total
    conf = _min_conf(estimate.get("confidence", CONF_LOW), _coverage_confidence(coverage))
    if reserve:
        conf = _down_one(conf)
    return _component(score, conf, coverage, reasons, readable, missing)


def auction_interestingness(car, board_stats):
    reasons, readable, missing = [], [], []
    pts, have, total = [], 0, 4

    w = _eng(car, "watchers")
    if w is not None:
        pr = _percentile_rank(board_stats.get("watchers") or [], w)
        if pr is not None:
            pts.append(pr * 32); have += 1
            reasons.append({"code": "watchers_percentile", "watchers": w, "percentile": round(pr, 3)})
            if pr >= 0.9:
                readable.append(f"Watchers in the top {round((1 - pr) * 100)}% of the board")
    else:
        missing.append("watchers")

    c = _eng(car, "comments")
    if c is not None:
        pr = _percentile_rank(board_stats.get("comments") or [], c)
        if pr is not None:
            pts.append(pr * 22); have += 1
            reasons.append({"code": "comments_percentile", "comments": c, "percentile": round(pr, 3)})
            if pr >= 0.9:
                readable.append("Unusually active comments")
    else:
        missing.append("comments")

    if _no_reserve(car):
        pts.append(8); have += 1
        reasons.append({"code": "no_reserve", "no_reserve": True})
        readable.append("No reserve")
    else:
        have += 1

    vi = _identity(car)
    if vi.get("originality") in ("modified", "replica") or vi.get("body_style") in ("targa",):
        pts.append(6); have += 1
        reasons.append({"code": "unusual_spec", "originality": vi.get("originality"), "body": vi.get("body_style")})
        readable.append("Unusual specification")

    # velocity needs real engagement history; the snapshot has a single point, so it's unavailable.
    missing.append("activity velocity (no engagement history yet)")

    score = int(round(_clamp(40 + sum(pts), 0, 100)))
    coverage = have / total
    return _component(score, _coverage_confidence(coverage), coverage, reasons, readable, missing)


def _min_conf(a, b):
    return a if _CONF_RANK.get(a, 0) <= _CONF_RANK.get(b, 0) else b


# ---------------------------------------------------------------------------
# opportunity score (market-only weighted combine) + tracking
# ---------------------------------------------------------------------------
def opportunity_score(components, *, weights=None):
    """Weighted combine over the components that produced a score. A missing component LOWERS
    confidence (its weight drops out and coverage falls) but never forces the score to zero. With
    too little present weight the score is None (we don't pretend to score on almost nothing)."""
    weights = weights or OPPORTUNITY_WEIGHTS
    num = den = 0.0
    present, confs = [], []
    for key, w in weights.items():
        comp = components.get(key) or {}
        s = comp.get("score")
        if isinstance(s, (int, float)) and not isinstance(s, bool):
            num += w * s
            den += w
            present.append(key)
            confs.append(comp.get("confidence", CONF_LOW))
    coverage = round(den, 3)                       # present weight in [0,1]
    if den < 0.5:                                   # less than half the weighted signal -> no honest score
        return {"score": None, "confidence": CONF_LOW if den > 0 else CONF_NONE,
                "coverage": coverage, "present": present}
    score = int(round(num / den))
    worst = min(confs, key=lambda c: _CONF_RANK.get(c, 0)) if confs else CONF_LOW
    conf = worst if den >= 0.85 else _down_one(worst)   # missing weight pulls confidence down a notch
    return {"score": score, "confidence": conf, "coverage": coverage, "present": present}


def tracking_status(car, estimate):
    if estimate.get("low") is None:
        return TRACK_TOO_EARLY
    bid = _num((car.get("bid") or {}).get("amount"))
    if bid is None:
        return TRACK_TOO_EARLY
    if bid < estimate["low"]:
        return TRACK_BELOW
    if bid > estimate["high"]:
        return TRACK_ABOVE
    return TRACK_NEAR


def _risk_flags(car, estimate, opp):
    flags = []
    if not _no_reserve(car):
        flags.append("reserve")
    if (car.get("details") or {}).get("tmu"):
        flags.append("tmu")
    cond = (car.get("details") or {}).get("condition") or []
    if any(f in RISK_CONDITION_FLAGS for f in cond):
        flags.append("condition-risk")
    vi = _identity(car)
    if vi.get("confidence") == CONF_LOW:
        flags.append("ambiguous-identity")
    if vi.get("originality") == "replica":          # a title-flagged replica/tribute/clone/kit
        flags.append("replica")
    if estimate.get("low") is None:
        flags.append("no-trusted-estimate")
    if opp.get("confidence") in (CONF_LOW, CONF_NONE):
        flags.append("low-confidence")
    return flags


def _summary_phrase(track, opp, interest_comp):
    """A single cautious, APPROVED phrase for analysis.summary."""
    if track == TRACK_TOO_EARLY:
        return APPROVED_PHRASES[TRACK_TOO_EARLY]
    score = opp.get("score")
    if track == TRACK_BELOW and isinstance(score, int) and score >= 60 and opp.get("confidence") in (CONF_HIGH, CONF_MEDIUM):
        return APPROVED_PHRASES["potential"]
    ic = interest_comp.get("score")
    if isinstance(ic, int) and ic >= 75:
        return APPROVED_PHRASES["high_interest"]
    if opp.get("confidence") == CONF_LOW:
        return APPROVED_PHRASES["low_confidence"]
    return APPROVED_PHRASES.get(track, APPROVED_PHRASES["watch"])


# ---------------------------------------------------------------------------
# the per-car entry point
# ---------------------------------------------------------------------------
def evaluate_car(car, comps, *, now, board_stats, now_iso=None, select=None):
    """Compute estimate + opportunity + analysis for one car. Returns the three blocks (the caller
    attaches them). Pure: reads only market fields already on the record + the comp pool."""
    est = estimate_range(car, comps, now=now, select=select)
    iq = investment_quality(car, comps, select=select)
    ea = enthusiast_appeal(car)
    bm = below_market_chance(car, est, now=now)
    ai = auction_interestingness(car, board_stats)
    components = {"investment_quality": iq, "enthusiast_appeal": ea,
                  "below_market_chance": bm, "auction_interestingness": ai}
    opp = opportunity_score(components)
    track = tracking_status(car, est)
    flags = _risk_flags(car, est, opp)

    opportunity = {
        "score": opp["score"], "confidence": opp["confidence"], "coverage": opp["coverage"],
        "tracking": track, "components": components,
        "readable": [APPROVED_PHRASES.get(track, APPROVED_PHRASES["watch"])],
        "weights_version": MODEL_VERSION,
    }
    analysis = {
        "score": opp["score"], "confidence": opp["confidence"],
        "summary": _summary_phrase(track, opp, ai),
        "basis": "opportunity-v6b", "flags": flags, "updated_at": now_iso,
    }
    return {"estimate": est, "opportunity": opportunity, "analysis": analysis}


# ---------------------------------------------------------------------------
# board-level scarce badge selection (req 17-20)
# ---------------------------------------------------------------------------
def build_board_stats(cars):
    """Sorted engagement distributions for percentile ranks (watchers/comments). Engagement only —
    it powers INTEREST, never the value range."""
    watchers = sorted(w for w in (_eng(c, "watchers") for c in cars) if w is not None)
    comments = sorted(c for c in (_eng(c, "comments") for c in cars) if c is not None)
    return {"watchers": watchers, "comments": comments}


def _main_candidate(car):
    """The (badge_code, driving_score) a car is ELIGIBLE for, before scarcity — or None. Diamond and
    Trophy require a confident, unambiguous identity (req 15); Diamond also needs a real below-market
    chance and a no-reserve, non-above-range posture (a reserve-driven 'discount' is not a Diamond)."""
    opp = car.get("opportunity") or {}
    comps = opp.get("components") or {}
    est = car.get("estimate") or {}
    score = opp.get("score")
    conf = opp.get("confidence")
    identity_ok = _identity_confident(car) and est.get("low") is not None

    cands = []
    # Diamond — opportunity
    bm = (comps.get("below_market_chance") or {}).get("score")
    if (identity_ok and isinstance(score, int) and conf in (CONF_HIGH, CONF_MEDIUM)
            and isinstance(bm, int) and bm >= DIAMOND_MIN_BELOW_MARKET
            and opp.get("tracking") in (TRACK_BELOW, TRACK_NEAR) and _no_reserve(car)):
        cands.append(("opportunity", score))
    # Trophy — investment quality + enthusiast appeal
    iq = (comps.get("investment_quality") or {}).get("score")
    ea = (comps.get("enthusiast_appeal") or {}).get("score")
    if (identity_ok and isinstance(iq, int) and isinstance(ea, int)
            and iq >= TROPHY_MIN_EACH and ea >= TROPHY_MIN_EACH):
        cands.append(("trophy", int(round((iq + ea) / 2))))
    # Flame — auction interestingness
    ai = (comps.get("auction_interestingness") or {}).get("score")
    if isinstance(ai, int):
        cands.append(("hot", ai))
    return cands


def assign_badges(cars):
    """Set car['badges'] across the whole board with scarcity: each badge needs an absolute floor AND
    a board percentile; mains are capped as a share of the board; one main per auction. Warning is a
    separate status flag driven by concrete risk. Returns a {code: count} tally."""
    live = [c for c in cars if c is not None]
    n = len(live)
    for c in live:
        c["badges"] = []
    if not n:
        return {}

    # board percentile tables for each badge's driving score
    by_code = {code: [] for code in BADGE_RULES}
    cand_map = {}
    for c in live:
        cand_map[id(c)] = _main_candidate(c)
        for code, sc in cand_map[id(c)]:
            by_code[code].append(sc)
    for code in by_code:
        by_code[code].sort()

    # each car's best ELIGIBLE main badge (absolute floor AND percentile), then one-per-auction by priority
    eligible = []   # (car, code, score)
    for c in live:
        best = None
        for code in MAIN_BADGE_PRIORITY:
            rule = BADGE_RULES[code]
            sc = next((s for cc, s in cand_map[id(c)] if cc == code), None)
            if sc is None or sc < rule["min_score"]:
                continue
            pr = _percentile_rank(by_code[code], sc)
            if pr is None or pr < rule["percentile"]:
                continue
            best = (c, code, sc)
            break       # MAIN_BADGE_PRIORITY order -> first qualifying wins (one main per auction)
        if best:
            eligible.append(best)

    # global cap: at most CAP_FRACTION of the board gets a main badge; keep the highest-scoring. Allow
    # at least one when any car qualifies, so a single legitimately-scarce badge on a small board isn't
    # dropped purely by int() flooring (on the full board the floor dominates).
    cap = max(1, int(n * MAIN_BADGE_CAP_FRACTION))
    eligible.sort(key=lambda t: t[2], reverse=True)
    tally = {}
    for c, code, _ in eligible[:max(cap, 0)]:
        c["badges"].append(code)
        tally[code] = tally.get(code, 0) + 1

    # Warning (status slot) — a CONCRETE vehicle risk (bad condition/title, TMU, a replica, or an
    # identity we couldn't confirm). A plain reserve auction or a thin comp pool is NOT a warning;
    # those are normal and shown elsewhere. Independent of the main-badge cap (it's a safety flag).
    for c in live:
        flags = (c.get("analysis") or {}).get("flags") or []
        risky = any(f in flags for f in ("condition-risk", "tmu", "ambiguous-identity", "replica"))
        if risky:
            c["badges"].append("warning")
            tally["warning"] = tally.get("warning", 0) + 1
    return tally
