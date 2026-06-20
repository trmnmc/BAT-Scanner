"""Comp-harvest tests (no network)."""

from scraper import comps


def _raw(id, title, year, bid, sold_text, sold_ts=1_700_000_000):
    return {
        "id": id, "title": title, "year": year, "current_bid": bid, "currency": "USD",
        "sold_text": sold_text, "sold_text_timestamp": sold_ts, "timestamp_end": sold_ts,
        "noreserve": False, "premium": False, "comments": None, "watchers": None,
        "views": None, "url": "https://bringatrailer.com/listing/x/", "thumbnail_url": "x",
    }


def test_parse_completed_sold_air_cooled():
    c = comps.parse_completed_item(
        _raw(1, "1985 Porsche 911 Carrera Coupe", "1985", 70000, "Sold for USD $70,000 on 1/2/2026"))
    assert c["make"] == "porsche" and c["model"] == "911" and c["year"] == 1985
    assert c["price"] == 70000
    assert "air-cooled-911-family" in c["category_ids"]


def test_parse_completed_skips_reserve_not_met():
    assert comps.parse_completed_item(
        _raw(2, "1989 Porsche 930 Turbo", "1989", 99000, "Bid to USD $99,000 on 1/2/2026")) is None


def test_parse_completed_skips_parts_and_non_category():
    # parts / no year
    assert comps.parse_completed_item(
        _raw(3, "Porsche 911 Wheels", None, 4900, "Sold for USD $4,900 on 1/2/2026")) is None
    # sold but not in any category
    assert comps.parse_completed_item(
        _raw(4, "2015 Honda Accord Sedan", "2015", 18000, "Sold for USD $18,000 on 1/2/2026")) is None


def test_harvest_dedupes_and_early_stops():
    pages = {
        1: {"items": [_raw(10, "1972 Chevrolet Chevelle SS 396", "1972", 60000, "Sold for USD $60,000 on x")]},
        2: {"items": [_raw(10, "dup", "1972", 60000, "Sold for USD $60,000 on x"),  # dup id
                      _raw(11, "1969 Pontiac GTO", "1969", 55000, "Sold for USD $55,000 on x")]},
        3: {"items": []}, 4: {"items": []}, 5: {"items": []},  # 3 empties -> stop
        6: {"items": [_raw(12, "should not reach", "1970", 1, "Sold for USD $1 on x")]},
    }
    got = comps.harvest_recent_sold(10, fetch_page=lambda p: pages.get(p, {"items": []}))
    ids = {c["id"] for c in got}
    assert ids == {10, 11}            # deduped, and stopped before page 6


def test_merge_dedupes_and_applies_retention():
    now = 2_000_000_000
    old = {"id": 1, "price": 1, "sold_ts": now - comps.RETENTION_SECONDS - 1, "category_ids": ["x"]}
    keep = {"id": 2, "price": 2, "sold_ts": now - 100, "category_ids": ["x"]}
    newer = {"id": 2, "price": 22, "sold_ts": now - 50, "category_ids": ["x"]}  # same id, wins
    merged = comps.merge_comps([old, keep], [newer], now=now)
    by_id = {c["id"]: c for c in merged}
    assert 1 not in by_id                       # aged out by retention
    assert by_id[2]["price"] == 22              # newer record won
