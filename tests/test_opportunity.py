"""Opportunity scoring / estimate-range / badge tests (no network)."""

import pytest

from scraper import opportunity as O

NOW = 1_800_000_000
H = 3600


def comp(price, *, year=1985, cmake="porsche", cmodel="911", cid=None):
    return {"id": cid if cid is not None else price, "price": price, "year": year,
            "title": f"{year} comp", "make": cmake, "model": cmodel,
            "canonical_make": cmake, "canonical_model": cmodel}


# a solid 8-comp pool around a $65k median
POOL = [comp(p) for p in (50000, 55000, 58000, 62000, 66000, 70000, 78000, 85000)]


def car(*, fair=66000, basis="make-model-y3", bid=42000, n=8, no_reserve=True, conf="high",
        appr=None, tilt=None, watchers=None, comments=None, ends_in_h=10, cats=None,
        body=None, engine=None, trans=None, drive=None, orig=None, condition=None, tmu=False,
        cmake="porsche", cmodel="911", year=1985, cid=1):
    details = None
    if condition is not None or tmu:
        details = {"tmu": tmu, "condition": condition or [], "miles": None}
    return {
        "id": cid, "year": year, "title": "1985 Porsche 911 Carrera",
        "make": {"slug": cmake, "name": cmake}, "models": [{"slug": cmodel, "name": cmodel}],
        "bid": {"amount": bid, "currency": "USD"},
        "flags": {"no_reserve": no_reserve},
        "engagement": {"watchers": watchers, "comments": comments, "views": None},
        "details": details,
        "category_ids": cats or [],
        "value": {"fair_value": fair, "basis": basis, "n_comps": n, "deal_pct": None,
                  "appreciation_pct": appr, "tilt": tilt, "is_deal": False},
        "vehicle_identity": {"canonical_make": cmake, "canonical_model": cmodel, "confidence": conf,
                             "body_style": body, "engine": engine, "transmission": trans,
                             "drivetrain": drive, "originality": orig},
        "_ends_ts": NOW + ends_in_h * H,
    }


# --- weights config (req 1, 2) -------------------------------------------------------------

def test_weights_total_one_and_are_validated():
    assert round(sum(O.OPPORTUNITY_WEIGHTS.values()), 6) == 1.0
    with pytest.raises(ValueError):
        O._validate_weights({"a": 0.5, "b": 0.4})


# --- component contract (req 3, 4) ---------------------------------------------------------

def _assert_component_shape(c):
    for k in ("score", "confidence", "coverage", "reasons", "readable", "missing"):
        assert k in c, f"component missing {k}"
    assert c["score"] is None or (isinstance(c["score"], int) and 0 <= c["score"] <= 100)
    assert c["confidence"] in ("high", "medium", "low", "none")
    assert isinstance(c["reasons"], list) and isinstance(c["readable"], list) and isinstance(c["missing"], list)


def test_every_component_returns_the_full_explainable_contract():
    full = car(appr=0.1, tilt=0.0, watchers=300, comments=40, body="coupe", engine="flat-6",
               trans="manual", drive="rwd", orig="original", condition=["numbers-matching"])
    est = O.estimate_range(full, POOL, now=NOW)
    bs = O.build_board_stats([full])
    for fn in (O.investment_quality(full, POOL), O.enthusiast_appeal(full),
               O.below_market_chance(full, est, now=NOW), O.auction_interestingness(full, bs)):
        _assert_component_shape(fn)


def test_missing_information_lowers_confidence_but_never_zeroes_the_score():
    sparse = car(appr=None, watchers=None, comments=None, conf="medium")  # little data
    iq = O.investment_quality(sparse, POOL[:2])     # only 2 comps -> thin
    assert iq["score"] is not None and iq["confidence"] in ("low", "medium")
    assert iq["missing"], "missing inputs are reported"
    # a fully-fed component is more confident than a starved one
    rich = O.investment_quality(car(appr=0.1, orig="original", condition=["numbers-matching"]), POOL)
    assert O._CONF_RANK[rich["confidence"]] >= O._CONF_RANK[iq["confidence"]]


# --- estimate range (req 12, 13, 14, 15) ---------------------------------------------------

def test_estimate_is_a_band_with_required_fields():
    est = O.estimate_range(car(), POOL, now=NOW)
    for k in ("low", "high", "currency", "confidence", "model_version", "comp_ids", "adjustment_reasons"):
        assert k in est
    assert est["low"] < est["high"] and est["currency"] == "USD"
    assert est["model_version"] == O.MODEL_VERSION
    assert len(est["comp_ids"]) >= 5 and est["adjustment_reasons"]


def test_estimate_never_below_current_bid():
    high_bid = O.estimate_range(car(bid=200000), POOL, now=NOW)   # bid above every comp
    assert high_bid["low"] >= 200000 and high_bid["high"] >= 200000


def test_estimate_never_collapses_to_a_point_when_bid_is_at_or_above_comps():
    # bid above every comp -> the band must stay a real band ABOVE the bid, never low == high (review fix)
    est = O.estimate_range(car(bid=200000), POOL, now=NOW)
    assert est["low"] >= 200000
    assert est["high"] > est["low"], "band must not collapse to a point"
    assert isinstance(est["low"], int) and isinstance(est["high"], int)
    assert any("re-floored above the bid" in r for r in est["adjustment_reasons"])
    # confidence is reduced versus a clean comp-anchored estimate
    clean = O.estimate_range(car(bid=20000), POOL, now=NOW)
    assert O._CONF_RANK[est["confidence"]] <= O._CONF_RANK[clean["confidence"]]


def test_band_floor_is_centered_on_the_tilted_median():
    # a tight pool + max upward tilt: the +/-12% floor must center on the TILTED median, keeping the
    # already-applied mileage/condition shift instead of snapping back to the raw median (review fix)
    tight = [comp(p) for p in (64000, 65000, 66000, 67000, 68000)]   # raw median 66000, very tight
    est = O.estimate_range(car(tilt=0.12, bid=0), tight, now=NOW)
    mid = (est["low"] + est["high"]) / 2
    assert mid > 66000 * 1.05, "floor must respect the +12% tilt, not collapse to the raw median"


def test_ambiguous_or_low_confidence_identity_gets_no_trusted_range():
    amb = O.estimate_range(car(conf="low"), POOL, now=NOW)
    assert amb["low"] is None and amb["high"] is None and amb["confidence"] == "none"
    untrusted = O.estimate_range(car(basis="insufficient"), POOL, now=NOW)
    assert untrusted["low"] is None


def test_reserve_auction_carries_separate_reserve_uncertainty_and_lower_confidence():
    nr = O.estimate_range(car(no_reserve=True), POOL, now=NOW)
    res = O.estimate_range(car(no_reserve=False), POOL, now=NOW)
    assert nr["reserve_uncertainty"] is None
    assert res["reserve_uncertainty"] and res["reserve_uncertainty"]["reserve"] is True
    assert O._CONF_RANK[res["confidence"]] < O._CONF_RANK[nr["confidence"]]


def test_watchers_and_comments_do_not_change_the_estimate(  ):
    quiet = O.estimate_range(car(watchers=None, comments=None), POOL, now=NOW)
    loud = O.estimate_range(car(watchers=5000, comments=900), POOL, now=NOW)
    assert (quiet["low"], quiet["high"]) == (loud["low"], loud["high"])


# --- below-market chance: a low EARLY bid is not an opportunity (req 9) ---------------------

def test_low_early_bid_is_not_an_opportunity_until_near_close():
    est = O.estimate_range(car(bid=42000), POOL, now=NOW)
    early = O.below_market_chance(car(bid=42000, ends_in_h=240), est, now=NOW)   # 10 days out
    late = O.below_market_chance(car(bid=42000, ends_in_h=2), est, now=NOW)      # about to close
    assert late["score"] > early["score"]
    assert early["score"] <= 62, "an early low bid must not read as a strong opportunity"


def test_reserve_halves_below_market_and_lowers_confidence():
    est_nr = O.estimate_range(car(bid=42000, ends_in_h=2, no_reserve=True), POOL, now=NOW)
    est_r = O.estimate_range(car(bid=42000, ends_in_h=2, no_reserve=False), POOL, now=NOW)
    nr = O.below_market_chance(car(bid=42000, ends_in_h=2, no_reserve=True), est_nr, now=NOW)
    res = O.below_market_chance(car(bid=42000, ends_in_h=2, no_reserve=False), est_r, now=NOW)
    assert res["score"] < nr["score"]


# --- opportunity score combine (req 4) -----------------------------------------------------

def test_opportunity_score_weights_present_components_and_drops_to_none_when_too_thin():
    comps = {"investment_quality": {"score": 80, "confidence": "high"},
             "enthusiast_appeal": {"score": 60, "confidence": "high"},
             "below_market_chance": {"score": 70, "confidence": "medium"},
             "auction_interestingness": {"score": 50, "confidence": "high"}}
    full = O.opportunity_score(comps)
    assert full["score"] == round(0.40 * 80 + 0.25 * 60 + 0.20 * 70 + 0.15 * 50)
    assert full["coverage"] == 1.0
    # drop two components -> confidence falls but score is still produced (not zeroed)
    partial = O.opportunity_score({"investment_quality": {"score": 80, "confidence": "high"},
                                   "enthusiast_appeal": {"score": 60, "confidence": "high"}})
    assert partial["score"] == round((0.40 * 80 + 0.25 * 60) / 0.65)
    assert O._CONF_RANK[partial["confidence"]] <= O._CONF_RANK[full["confidence"]]
    # almost nothing present -> no honest score
    none = O.opportunity_score({"auction_interestingness": {"score": 90, "confidence": "high"}})
    assert none["score"] is None


def test_opportunity_score_is_market_only_personal_fields_are_ignored():
    base = car(watchers=300, comments=40, appr=0.1, body="coupe", trans="manual")
    bs = O.build_board_stats([base])
    a = O.evaluate_car(base, POOL, now=NOW, board_stats=bs)
    # injecting personal data must not change the market score (the engine never reads it)
    personal = dict(base, _watchlist_match=True, user_state={"status": "bid_plan"}, max_bid=999999, notes="dad loves it")
    b = O.evaluate_car(personal, POOL, now=NOW, board_stats=bs)
    assert a["opportunity"]["score"] == b["opportunity"]["score"]


# --- tracking statuses (req 16) ------------------------------------------------------------

def test_tracking_statuses():
    est = O.estimate_range(car(), POOL, now=NOW)
    lo, hi = est["low"], est["high"]
    assert O.tracking_status(car(bid=lo - 5000), est) == O.TRACK_BELOW
    assert O.tracking_status(car(bid=(lo + hi) // 2), est) == O.TRACK_NEAR
    assert O.tracking_status(car(bid=hi + 50000), est) == O.TRACK_ABOVE
    assert O.tracking_status(car(conf="low"), O.estimate_range(car(conf="low"), POOL, now=NOW)) == O.TRACK_TOO_EARLY


def test_summary_phrase_is_always_from_the_approved_list():
    allowed = set(O.APPROVED_PHRASES.values())
    for kw in [dict(), dict(conf="low"), dict(basis="insufficient"), dict(bid=20000, ends_in_h=2, watchers=900),
               dict(no_reserve=False), dict(watchers=5000, comments=900)]:
        c = car(**kw)
        bs = O.build_board_stats([c])
        res = O.evaluate_car(c, POOL, now=NOW, board_stats=bs)
        assert res["analysis"]["summary"] in allowed


# --- scarce production badges (req 15, 17, 18, 19, 20) -------------------------------------

def _board_with(*cars):
    bs = O.build_board_stats(list(cars))
    for c in cars:
        res = O.evaluate_car(c, POOL, now=NOW, board_stats=bs, now_iso="2026-06-24T00:00:00Z")
        c["estimate"], c["opportunity"], c["analysis"] = res["estimate"], res["opportunity"], res["analysis"]
    return list(cars)


def test_ambiguous_identity_never_gets_diamond_or_trophy():
    # a car that would otherwise be a strong opportunity, but with a low-confidence identity
    amb = car(cid=1, conf="low", bid=42000, ends_in_h=2, appr=0.1, trans="manual", body="coupe",
              watchers=900, comments=200)
    O._board_assign = None
    board = _board_with(amb, car(cid=2, bid=60000), car(cid=3, bid=61000))
    O.assign_badges(board)
    assert "opportunity" not in amb["badges"] and "trophy" not in amb["badges"]


def test_one_main_badge_per_auction_and_cap_limits_share():
    # 20 strong opportunity-shaped cars; the cap (~12%) must keep mains scarce, one main each
    cars = []
    for i in range(20):
        cars.append(car(cid=100 + i, bid=40000 + i * 100, ends_in_h=2, no_reserve=True, appr=0.12,
                        trans="manual", body="coupe", engine="flat-6", orig="original",
                        condition=["numbers-matching"], watchers=400 + i, comments=60 + i))
    board = _board_with(*cars)
    tally = O.assign_badges(board)
    main_holders = [c for c in board if any(b in ("opportunity", "trophy", "hot") for b in c["badges"])]
    assert len(main_holders) <= int(20 * O.MAIN_BADGE_CAP_FRACTION) + 1
    for c in board:
        mains = [b for b in c["badges"] if b in ("opportunity", "trophy", "hot")]
        assert len(mains) <= 1, "at most one main badge per auction"


def test_warning_badge_flags_concrete_risk():
    risky = car(cid=7, tmu=True, condition=["salvage-title"], conf="high")
    board = _board_with(risky, car(cid=8, bid=60000), car(cid=9, bid=61000))
    O.assign_badges(board)
    assert "warning" in risky["badges"]
