"""Tests for the quota-based, category-agnostic enrichment target selection."""

from scraper.__main__ import _select_enrichment_targets

NOW = 1_750_000_000  # fixed unix time for determinism
H = 3600


def rec(id_, *, ends_h=24, eng=False, det=False, deal=False, no_reserve=False,
        eng_age_h=None, category_ids=None):
    """A scored record. ends_h hours-from-NOW; eng/det = has cached data; deal = trusted
    no-reserve deal candidate; eng_age_h = how old the engagement timestamp is."""
    value = {"basis": "insufficient", "deal_pct": None}
    if deal:
        value = {"basis": "make-model-y3", "deal_pct": 0.3}
        no_reserve = True
    enrichment = None
    if eng_age_h is not None:
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(NOW - eng_age_h * H, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        enrichment = {"engagement_updated_at": ts, "details_updated_at": ts}
    return {
        "id": id_,
        "_ends_ts": NOW + ends_h * H,
        "category_ids": category_ids if category_ids is not None else [],
        "flags": {"no_reserve": no_reserve},
        "value": value,
        "engagement": {"comments": 5, "watchers": 9} if eng else {"comments": None, "watchers": None},
        "details": {"miles": 1000, "condition": []} if det else None,
        "enrichment": enrichment,
    }


def ids(records):
    return [r["id"] for r in records]


def test_categories_have_no_effect():
    base = [rec(i, ends_h=10, eng=(i % 2 == 0)) for i in range(50)]
    tagged = [dict(r, category_ids=["air-cooled-911-family", "vintage-trucks"]) for r in base]
    a = ids(_select_enrichment_targets(base, NOW))
    b = ids(_select_enrichment_targets(tagged, NOW))
    assert a == b, "placeholder category tags must not change selection"


def test_cap_respected_and_ids_unique():
    cars = [rec(i, ends_h=5) for i in range(500)]  # 500 all ending soon
    out = _select_enrichment_targets(cars, NOW)
    assert len(out) == 300
    assert len(set(ids(out))) == 300, "no duplicate ids"


def test_urgent_deal_candidates_come_first():
    # one trusted deal candidate ending far out + many soon non-candidates
    cars = [rec(1, ends_h=100, deal=True)] + [rec(i, ends_h=2) for i in range(2, 60)]
    out = _select_enrichment_targets(cars, NOW, cap=1)
    assert ids(out) == [1], "deal candidate is taken before soonest-ending non-candidates"


def test_unenriched_get_reserved_slots_even_when_many_end_soon():
    # 200 ending-soon but already enriched -> urgent; 50 unenriched ending far -> not urgent
    soon_enriched = [rec(i, ends_h=2, eng=True, eng_age_h=1) for i in range(200)]
    unenriched_far = [rec(1000 + i, ends_h=200) for i in range(50)]  # eng/det False -> unenriched
    out = _select_enrichment_targets(soon_enriched + unenriched_far, NOW)
    sel = set(ids(out))
    assert len(out) <= 300
    # all 50 unenriched got in via their reserved quota
    assert all((1000 + i) in sel for i in range(50)), "unenriched reserved slots honored"


def test_stale_get_reserved_slots():
    soon_enriched = [rec(i, ends_h=2, eng=True, eng_age_h=1) for i in range(200)]
    stale = [rec(2000 + i, ends_h=200, eng=True, eng_age_h=100) for i in range(30)]  # 100h > 72h
    out = _select_enrichment_targets(soon_enriched + stale, NOW)
    sel = set(ids(out))
    stale_selected = sum(1 for i in range(30) if (2000 + i) in sel)
    assert stale_selected >= 20, "stale bucket's reserved quota is honored"


def test_rotating_sample_is_deterministic_and_cycles():
    # all enriched + recent + not ending soon -> only the sample bucket has candidates
    cars = [rec(i, ends_h=500, eng=True, eng_age_h=1) for i in range(100)]
    day1 = ids(_select_enrichment_targets(cars, NOW, cap=10))
    day1b = ids(_select_enrichment_targets(cars, NOW, cap=10))
    assert day1 == day1b, "same now -> identical selection (deterministic)"
    two_days_later = NOW + 2 * 86400
    day3 = ids(_select_enrichment_targets(cars, two_days_later, cap=10))
    assert set(day1) != set(day3), "a later UTC day rotates the sample to different records"
