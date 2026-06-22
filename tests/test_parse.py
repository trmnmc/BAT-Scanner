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


# --- listing details: mileage + condition (Phase 3) ---

def _details_html(*bullets):
    lis = "".join(f"<li>{b}</li>" for b in bullets)
    return ('<div class="essentials"><div class="item"><strong>Listing Details</strong>'
            f"<ul>{lis}</ul></div></div>")


def test_parse_listing_details_from_fixture():
    html = (FIXTURES / "bat_listing.html").read_text(encoding="utf-8")
    d = parse.parse_listing_details(html, "1985 Porsche 911 Carrera Coupe")
    assert d["miles"] == 45000
    assert d["odometer_raw"] == "45k Miles Shown"
    assert d["tmu"] is False
    assert "numbers-matching" in d["condition"]
    assert "repaint" in d["condition"]


@pytest.mark.parametrize("bullet,miles,tmu", [
    ("80k Miles Shown", 80000, False),
    ("7k Miles Shown, TMU", 7000, True),
    ("45,300 Miles", 45300, False),
    ("243k Kilometers (~151k Miles) Shown", 151000, False),   # prefer the converted miles figure
    ("180,000 Kilometers", round(180000 * 0.621371), False),  # km-only -> converted
])
def test_parse_odometer_variants(bullet, miles, tmu):
    d = parse.parse_listing_details(_details_html("Chassis: X", bullet, "3.2L Flat-Six"))
    assert d["miles"] == miles
    assert d["tmu"] is tmu


def test_parse_details_no_block():
    d = parse.parse_listing_details("<html><body>nothing useful</body></html>", "1990 Whatever")
    assert d == {"miles": None, "odometer_raw": None, "tmu": False, "condition": []}


def test_parse_details_ignores_related_listing_noise():
    # mileage figures outside the Listing Details block (sidebar/comments) must be ignored
    html = _details_html("80k Miles Shown") + '<div class="similar">45k-Mile 1965 Mustang</div>'
    assert parse.parse_listing_details(html)["miles"] == 80000


def test_condition_no_false_positives_on_colors():
    # the over-exclusion lesson: paint/color names must not trip condition flags
    d = parse.parse_listing_details(_details_html("Shell Grey Paint", "Grand Prix White Paint",
                                                  "80k Miles Shown"), "1973 Porsche 911T")
    assert d["condition"] == []
    assert d["miles"] == 80000


@pytest.mark.parametrize("title,bullet,flag", [
    ("1972 Datsun 240Z Replica", "Chassis: X", "replica"),
    ("1969 Ford Mustang", "Rebuilt 302ci Engine", "rebuilt-engine"),
    ("1965 Ford Mustang Restomod", "Coyote V8", "restomod"),
    ("1980 MG Tribute", "Chassis: X", "tribute"),
    ("1985 Porsche 911", "Numbers-Matching Drivetrain", "numbers-matching"),
    ("K20-Powered 1983 Nissan Skyline", "Chassis: X", "engine-swap"),   # BaT "-Powered" = swap
    ("Twin-Turbocharged 408-Powered 1970 Ford F-100", "Chassis: X", "engine-swap"),
])
def test_condition_flags(title, bullet, flag):
    d = parse.parse_listing_details(_details_html(bullet), title)
    assert flag in d["condition"]
