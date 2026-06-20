"""Fair-value / deal / appreciation tests (no network)."""

from scraper import value

NOW = 1_800_000_000
DAY = 86400


def _car(*, price, year=1985, ends_in_h=1):
    return {
        "make": {"slug": "porsche"}, "models": [{"slug": "911"}], "year": year,
        "category_ids": ["air-cooled-911-family"],
        "bid": {"amount": price, "currency": "USD", "status": "live"},
        "_ends_ts": NOW + ends_in_h * 3600,
    }


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


def test_appreciation_recent_vs_older():
    older = [_comp(p, days_ago=400) for p in (40000, 42000, 44000)]    # median 42000
    recent = [_comp(p, days_ago=30) for p in (50000, 52000, 54000)]    # median 52000
    v = value.compute_value(_car(price=48000), older + recent, now=NOW)
    assert v["appreciation_pct"] == round((52000 - 42000) / 42000, 4)  # ~0.238
