"""Parser tests — run against the committed fixtures (no network)."""

from pathlib import Path

import pytest

from scraper import parse

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def board_html():
    return (FIXTURES / "bat_auctions.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def raw_items(board_html):
    return parse.parse_auctions_html(board_html)


def _by_id(items, iid):
    return next(i for i in items if i["id"] == iid)


def test_extract_and_count(raw_items):
    assert len(raw_items) == 13


def test_extract_ignores_decoy_marker():
    # a textual mention of the name before the real assignment must not hijack it
    html = (
        '<script>console.log("auctionsCurrentInitialData loaded"); var cfg = {};</script>'
        '<script>var auctionsCurrentInitialData = {"items":[{"id":1,"title":"x"}]};</script>'
    )
    items = parse.parse_auctions_html(html)
    assert len(items) == 1 and items[0]["id"] == 1


def test_extract_missing_blob_raises():
    import pytest as _pytest
    with _pytest.raises(ValueError):
        parse.parse_auctions_html("<html><body>nothing here</body></html>")


def test_parse_item_air_cooled_record(raw_items):
    rec = parse.parse_item(_by_id(raw_items, 116070319))
    assert rec["id"] == 116070319
    assert rec["year"] == 1985 and isinstance(rec["year"], int)
    assert rec["bid"] == {"amount": 70500, "currency": "USD", "status": "live"}
    assert rec["make"]["name"] == "Porsche"
    assert rec["make"]["slug"] == "porsche"
    assert rec["make"]["id"] is None
    assert rec["ends_at"] == "2100-01-01T00:00:00Z"
    assert rec["started_at"] is None            # not exposed by source
    assert rec["flags"] == {"no_reserve": True, "premium": False, "alumni": None}
    assert rec["engagement"] == {"comments": None, "views": None, "watchers": None}
    assert rec["listing_url"].startswith("https://bringatrailer.com/listing/")
    assert rec["thumbnail_url"].endswith(".jpg")
    assert rec["category_ids"] == []            # filled later by categories


def test_status_live_vs_ended(raw_items):
    live = parse.parse_item(_by_id(raw_items, 116070319))
    ended = parse.parse_item(_by_id(raw_items, 200000030))
    assert live["bid"]["status"] == "live"
    assert ended["bid"]["status"] == "sold"     # sold_text = "Sold for ..."
    assert parse.is_live(_by_id(raw_items, 116070319))
    assert not parse.is_live(_by_id(raw_items, 200000030))


@pytest.mark.parametrize("value,expected", [
    (None, None),
    ("0", 0),
    ("44", 44),
    ("8 Watchers", 8),
    ("5,300 Views", 5300),
    (1234, 1234),
    ("", None),
    ("n/a", None),
    (True, None),
])
def test_parse_engagement_value(value, expected):
    assert parse.parse_engagement_value(value) == expected


@pytest.mark.parametrize("value,expected", [
    (136000, 136000),
    ("136000", 136000),
    ("USD $136,000", 136000),
    (None, None),
    ("", None),
    (True, None),
])
def test_parse_money(value, expected):
    assert parse.parse_money(value) == expected


def test_unix_to_iso():
    assert parse.unix_to_iso(4102444800) == "2100-01-01T00:00:00Z"
    assert parse.unix_to_iso(None) is None
    assert parse.unix_to_iso(0) is None


def test_parse_year_from_title_fallback():
    assert parse.parse_year({"year": "1985"}) == 1985
    assert parse.parse_year({"year": None, "title": "1973 Porsche 911 Carrera RS"}) == 1973
    assert parse.parse_year({"year": "", "title": "no year here"}) is None


def test_derive_make_models_single_and_multiword():
    make, models, taxo = parse.derive_make_models("11k-Mile 1998 Ferrari 355 F1 Berlinetta")
    assert make["name"] == "Ferrari" and make["slug"] == "ferrari"
    assert models and models[0]["name"] == "355"
    assert taxo == ["ferrari/355"]

    make2, _, _ = parse.derive_make_models("1969 Alfa Romeo GTV 1750")
    assert make2["name"] == "Alfa Romeo" and make2["slug"] == "alfa-romeo"


def test_parse_listings_filter_join_map():
    text = (FIXTURES / "bat_listings_filter.json").read_text(encoding="utf-8")
    eng = parse.parse_listings_filter(text)
    assert eng[116070319] == {"comments": 44, "watchers": 210, "views": 5300}
    assert eng[200000004]["comments"] == 120
    assert 999999001 in eng  # join layer is responsible for filtering, not the parser


def test_parse_listing_engagement():
    html = (FIXTURES / "bat_listing.html").read_text(encoding="utf-8")
    eng = parse.parse_listing_engagement(html)
    assert eng == {"comments": 56, "views": None, "watchers": 1728}


def test_parse_listing_engagement_missing_fields():
    eng = parse.parse_listing_engagement("<html><body>no stats here</body></html>")
    assert eng == {"comments": None, "views": None, "watchers": None}
