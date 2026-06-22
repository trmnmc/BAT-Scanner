"""Fair-value / deal / appreciation tests (no network)."""

from scraper import value

NOW = 1_800_000_000
DAY = 86400


def _car(*, price, year=1985, ends_in_h=1, details=None, no_reserve=True):
    return {
        "make": {"slug": "porsche"}, "models": [{"slug": "911"}], "year": year,
        "category_ids": ["air-cooled-911-family"],
        "bid": {"amount": price, "currency": "USD", "status": "live"},
        "flags": {"no_reserve": no_reserve},
        "details": details,
        "_ends_ts": NOW + ends_in_h * 3600,
    }


_FIVE = (40000, 50000, 60000, 70000, 80000)  # median 60000


def _comp(price, *, year=1985, days_ago=30, make="porsche", model="911",
          cats=("air-cooled-911-family",)):
    return {"make": make, "model": model, "year": year, "price": price,
            "sold_ts": NOW - days_ago * DAY, "category_ids": list(cats)}


def test_fair_value_is_median_and_deal_pct():
    comps = [_comp(p) for p in (40000, 50000, 60000, 70000, 80000)]  # median 60000
    v = value.compute_value(_car(price=42000), comps, now=NOW)
    assert v["fair_value"] == 60000
    assert v["n_comps"] == 5 and v["basis"] == "make-model-y3"
    assert v["deal_pct"] == round((60000 - 42000) / 60000, 4)        # 0.30


def test_is_deal_requires_ending_soon():
    comps = [_comp(p) for p in (50000, 55000, 60000, 65000, 70000)]  # median 60000
    cheap_soon = value.compute_value(_car(price=40000, ends_in_h=2), comps, now=NOW)
    cheap_later = value.compute_value(_car(price=40000, ends_in_h=200), comps, now=NOW)
    assert cheap_soon["is_deal"] is True      # cheap AND ending soon
    assert cheap_later["is_deal"] is False     # cheap but days away -> may still bid up


def test_reserve_auction_never_flagged_deal():
    # a cheap, ending-soon, well-comped car on a RESERVE auction is NOT a deal: the bid is
    # below an unmet reserve, not a real price. Same car no-reserve IS a deal.
    comps = [_comp(p) for p in (50000, 55000, 60000, 65000, 70000)]   # median 60000
    reserve = value.compute_value(_car(price=20000, ends_in_h=2, no_reserve=False), comps, now=NOW)
    nores = value.compute_value(_car(price=20000, ends_in_h=2, no_reserve=True), comps, now=NOW)
    assert reserve["deal_pct"] == nores["deal_pct"]   # same raw under-comps math
    assert reserve["is_deal"] is False                # but reserve bid isn't a price
    assert nores["is_deal"] is True


def test_not_a_deal_when_not_cheap():
    comps = [_comp(p) for p in (50000, 55000, 60000, 65000, 70000)]
    v = value.compute_value(_car(price=59000, ends_in_h=1), comps, now=NOW)
    assert v["is_deal"] is False               # within ~2% of median, not a deal


def test_insufficient_comps_gates_deal():
    comps = [_comp(50000), _comp(60000)]       # only 2 (< MIN_COMPS)
    v = value.compute_value(_car(price=20000, ends_in_h=1), comps, now=NOW)
    assert v["n_comps"] == 2 and v["basis"] == "insufficient"
    assert v["is_deal"] is False               # never flag a deal on thin comps


def test_year_band_widens_then_category_fallback():
    # no same-year comps within 3y, but several within 7y -> tier 2
    comps = [_comp(p, year=1979) for p in (40000, 45000, 50000, 55000, 60000)]
    v = value.compute_value(_car(price=30000, year=1985), comps, now=NOW)
    assert v["basis"] == "make-model-y7" and v["n_comps"] == 5


def test_no_category_fallback():
    # 8 comps in the same category but a DIFFERENT model must not produce a fair value
    # for a 911 (no whole-category blending).
    other_model = [_comp(40000, model="912") for _ in range(8)]
    v = value.compute_value(_car(price=20000, ends_in_h=1), other_model, now=NOW)
    assert v["basis"] == "insufficient"
    assert v["fair_value"] is None
    assert v["is_deal"] is False


def test_tilt_zero_without_details_and_score_equals_pct():
    comps = [_comp(p) for p in _FIVE]
    v = value.compute_value(_car(price=42000), comps, now=NOW)   # details=None
    assert v["tilt"] == 0.0
    assert v["deal_score"] == v["deal_pct"]                      # no nudge -> score == pct


def test_low_mileage_raises_score_high_mileage_lowers_it():
    comps = [_comp(p) for p in _FIVE]
    clean = {"miles": 0, "tmu": False, "condition": []}
    worn = {"miles": 840000, "tmu": False, "condition": []}      # ~20k mi/yr over 42y
    lo = value.compute_value(_car(price=42000, details=clean), comps, now=NOW)
    hi = value.compute_value(_car(price=42000, details=worn), comps, now=NOW)
    assert lo["tilt"] > 0 and hi["tilt"] < 0
    assert lo["deal_score"] > lo["deal_pct"] > hi["deal_score"]


def test_tmu_ignores_mileage():
    comps = [_comp(p) for p in _FIVE]
    v = value.compute_value(
        _car(price=42000, details={"miles": 0, "tmu": True, "condition": []}), comps, now=NOW)
    assert v["tilt"] == 0.0                                      # TMU -> can't trust the number


def test_bad_condition_lowers_score_missing_is_never_penalized():
    comps = [_comp(p) for p in _FIVE]
    bad = value.compute_value(
        _car(price=42000, details={"miles": None, "tmu": False, "condition": ["restomod", "replica"]}),
        comps, now=NOW)
    none = value.compute_value(_car(price=42000, details={"miles": None, "tmu": False, "condition": []}),
                               comps, now=NOW)
    assert bad["tilt"] < 0
    assert none["tilt"] == 0.0                                   # absent data is 0, not a penalty


def test_tilt_none_when_not_scoreable():
    comps = [_comp(50000), _comp(60000)]                         # 2 comps -> insufficient
    v = value.compute_value(
        _car(price=20000, details={"miles": 0, "tmu": False, "condition": []}), comps, now=NOW)
    assert v["basis"] == "insufficient"
    assert v["tilt"] is None and v["deal_score"] is None         # no score on thin comps


def test_tilt_clamped_to_limit():
    comps = [_comp(p) for p in _FIVE]
    # +0.06 mileage + capped good condition; total stays within the clamp
    v = value.compute_value(
        _car(price=42000, details={"miles": 0, "tmu": False,
                                   "condition": ["numbers-matching", "original-paint"]}),
        comps, now=NOW)
    assert 0 < v["tilt"] <= value.TILT_CLAMP


def test_is_deal_unaffected_by_tilt():
    comps = [_comp(p) for p in (50000, 55000, 60000, 65000, 70000)]  # median 60000
    worn = {"miles": 999000, "tmu": False, "condition": ["restomod"]}
    v = value.compute_value(_car(price=40000, ends_in_h=2, details=worn), comps, now=NOW)
    assert v["is_deal"] is True          # is_deal keys off deal_pct + ending soon, not the tilt
    assert v["tilt"] < 0


def test_appreciation_recent_vs_older():
    older = [_comp(p, days_ago=400) for p in (40000, 42000, 44000)]    # median 42000
    recent = [_comp(p, days_ago=30) for p in (50000, 52000, 54000)]    # median 52000
    v = value.compute_value(_car(price=48000), older + recent, now=NOW)
    assert v["appreciation_pct"] == round((52000 - 42000) / 42000, 4)  # ~0.238
