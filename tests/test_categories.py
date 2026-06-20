"""Category-matching tests for air-cooled-911-family."""

from pathlib import Path

import pytest

from scraper import categories, parse

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AIRCOOLED = categories.CATEGORY_AIR_COOLED_911


def _record(title, year, make_name="Porsche"):
    """Minimal normalized record for predicate tests."""
    return {
        "title": title,
        "year": year,
        "make": {"id": None, "name": make_name, "slug": make_name.lower()},
    }


@pytest.fixture(scope="module")
def live_records():
    html = (FIXTURES / "bat_auctions.html").read_text(encoding="utf-8")
    recs = [parse.parse_item(r) for r in parse.parse_auctions_html(html)]
    return [r for r in recs if r["bid"]["status"] == "live"]


def test_fixture_matches_exact_set(live_records):
    matched = {r["id"] for r in live_records if categories.match_categories(r)}
    assert matched == {116070319, 200000001, 200000002, 200000003, 200000004}


def test_post_1998_911_not_matched(live_records):
    rec = next(r for r in live_records if r["id"] == 200000010)  # 2006 911
    assert categories.match_categories(rec) == []


def test_non_porsche_not_matched(live_records):
    rec = next(r for r in live_records if r["id"] == 200000011)  # Ferrari 355
    assert categories.match_categories(rec) == []


@pytest.mark.parametrize("title", [
    "1970 Porsche 911 Go-Kart",
    "1972 Porsche 911 Parts Lot",
    "1973 Porsche 911 Carrera Engine Only",
    "1973 Porsche 930 Complete Engine",
    "1980 Porsche 911 SC Roller",
    "1973 Porsche 911 Carrera RS Tribute",
    "1974 Porsche 911 Replica",
    "1975 Porsche 911 Re-Creation",
    "1972 Porsche 911 Body Shell",
])
def test_exclusion_keywords(title):
    year = int(title.split()[0])
    assert categories.matches_air_cooled_911(_record(title, year)) is False


@pytest.mark.parametrize("title", [
    "1980 Porsche 911SC Coupe with Rebuilt Engine",
    "1973 Porsche 911 Carrera, Numbers-Matching Engine",
    "1995 Porsche 993 Carrera 4 Shell Grey",
    "1988 Porsche 911 Carrera, Part of a Private Collection",
])
def test_legit_cars_not_over_excluded(title):
    # bare "engine"/"shell"/"part" must NOT drop a whole car (regression for the
    # over-broad exclusion patterns).
    year = int(title.split()[0])
    assert categories.matches_air_cooled_911(_record(title, year)) is True


@pytest.mark.parametrize("year,expected", [
    (1963, False),  # before air-cooled 911 era
    (1964, True),
    (1998, True),
    (1999, False),  # 996 / water-cooled era
    (2006, False),
])
def test_year_boundaries(year, expected):
    rec = _record(f"{year} Porsche 911 Carrera Coupe", year)
    assert categories.matches_air_cooled_911(rec) is expected


def test_generation_required():
    # 914 is air-cooled but NOT in the 911 family -> must not match.
    assert categories.matches_air_cooled_911(_record("1975 Porsche 914 2.0", 1975)) is False
    # 911 family members do match.
    for gen in ("911", "912", "930", "964", "993"):
        assert categories.matches_air_cooled_911(_record(f"1985 Porsche {gen} Coupe", 1985)) is True


def test_make_must_be_porsche():
    # "911" in a non-Porsche title must not match.
    assert categories.matches_air_cooled_911(_record("1985 Acme 911 Coupe", 1985, make_name="Acme")) is False


def test_missing_year_does_not_crash():
    assert categories.matches_air_cooled_911(_record("Porsche 911 Carrera", None)) is False


def _rec_from_title(title):
    """Build a normalized record from just a title (year + make derived)."""
    raw = {
        "title": title, "year": None, "current_bid": 0, "currency": "USD",
        "timestamp_end": 4102444800, "sold_text": "", "noreserve": False,
        "premium": False, "comments": None, "watchers": None, "views": None,
        "url": "https://bringatrailer.com/listing/x/", "thumbnail_url": "x",
    }
    return parse.parse_item(raw)


def test_registry_has_five_categories():
    assert len(categories.CATEGORY_IDS) == 5
    assert set(categories.CATEGORY_IDS) == {
        "air-cooled-911-family", "60s-muscle", "80s-90s-japanese",
        "vintage-trucks", "german-wagons",
    }


# --- per-category match / no-match (titles from the category research) --------------

_CASES = {
    "60s-muscle": {
        "match": [
            "11k-Mile 1969 Chevrolet Chevelle SS 396 4-Speed",
            "1970 Plymouth Hemi 'Cuda 4-Speed",
            "1969 Dodge Charger R/T 440 Six Pack",
            "1967 Pontiac GTO 400 4-Speed",
            "1965 Shelby GT350 Fastback",
            "1970 Buick GSX Stage 1",
            "1970 Oldsmobile 442 W-30 Convertible",
            "1969 Chevrolet Camaro Z/28 RS",
            "1968 Ford Mustang GT390 Fastback",
            "1970 Plymouth Superbird 440",
            "1969 AMC AMX 390 4-Speed",
            "1970 Dodge Challenger R/T 440 Magnum",
        ],
        "nomatch": [
            "1974 Ford Mustang II Ghia Coupe",
            "1978 Pontiac Firebird Trans Am Y82",
            "1985 Oldsmobile Cutlass Supreme Brougham",
            "1969 Chevrolet Chevelle SS 396 Tribute",
            "1965 Shelby GT350 Replica",
            "1972 Chevrolet El Camino Parts Lot",
            "1969 Volkswagen Beetle",
            "1967 Chevrolet Corvette 427 Coupe",
            "1968 Ford Falcon Wagon",
            "1973 Plymouth Duster Slant-Six",
        ],
    },
    "80s-90s-japanese": {
        "match": [
            "27k-Mile 1994 Toyota Supra Turbo 6-Speed",
            "1985 Toyota Corolla GT-S Coupe AE86",
            "1991 Toyota MR2 Turbo",
            "1993 Mazda RX-7 Touring",
            "1990 Mazda MX-5 Miata",
            "1991 Acura NSX 5-Speed",
            "1989 Nissan Skyline GT-R",
            "1990 Nissan 300ZX Twin Turbo",
            "1998 Nissan 240SX SE",
            "1992 Honda Civic Si Hatchback",
            "1988 Honda CRX Si",
            "1995 Acura Integra GS-R",
            "1999 Mitsubishi 3000GT VR-4",
            "1988 Mitsubishi Starion ESI-R",
            "1998 Subaru Impreza 2.5 RS",
            "1994 Toyota Land Cruiser FJ80",
            "1983 Datsun 280ZX Turbo",
            "1997 Lexus SC300 5-Speed",
        ],
        "nomatch": [
            "1990 Toyota Camry DX Sedan",
            "1995 Honda Accord LX",
            "1992 Nissan Sentra Sedan",
            "1988 Toyota Pickup SR5",
            "1994 Mazda 626 ES",
            "2003 Mazda RX-8 6-Speed",
            "1979 Datsun 280ZX Coupe",
            "2002 Acura RSX Type-S",
            "1996 Toyota Supra Turbo Engine Only",
            "1991 Mazda Miata Parts Car",
            "1985 Chevrolet Corvette Coupe",
        ],
    },
    "vintage-trucks": {
        "match": [
            "1969 Ford F-100 Ranger 4x4",
            "1966 Ford Bronco U13 Roadster",
            "23k-Mile 1972 Chevrolet K5 Blazer",
            "1965 Chevrolet C10 Stepside Pickup",
            "1987 GMC Suburban 1500",
            "1975 Dodge Power Wagon W200 4x4",
            "1979 Dodge Ramcharger SE 4x4",
            "1978 Toyota Land Cruiser FJ40",
            "1992 Toyota Land Cruiser FJ80",
            "1985 Toyota Pickup SR5 4x4",
            "1971 Land Rover Series IIA 88",
            "1990 Land Rover Defender 110",
            "1988 Jeep Grand Wagoneer",
            "1979 Jeep CJ-7 Renegade",
            "1973 International Harvester Scout II",
            "1983 Datsun 720 King Cab 4x4",
        ],
        "nomatch": [
            "2021 Ford Bronco First Edition",
            "2023 Jeep Gladiator Rubicon",
            "2020 Land Rover Defender 110 X",
            "2008 Toyota FJ Cruiser",
            "1998 Toyota Land Cruiser 4WD",
            "2015 Chevrolet Suburban LTZ",
            "1969 Chevrolet Chevelle SS 396 4-Speed",
            "1972 Datsun 240Z Coupe",
            "1965 Ford Mustang Fastback",
            "1985 Toyota Land Cruiser FJ60 Parts Lot",
        ],
    },
    "german-wagons": {
        "match": [
            "1985 Mercedes-Benz 300TD Turbodiesel Wagon",
            "1989 Mercedes-Benz 300TE Estate",
            "1995 Mercedes-Benz E320 Wagon",
            "2004 Audi RS6 Avant",
            "2013 Audi S4 Avant 6-Speed",
            "2001 Audi S6 Avant",
            "37k-Mile 2018 Audi RS4 Avant",
            "2008 BMW 535xi Sports Wagon",
            "1998 BMW 528i Touring",
            "2025 BMW M5 Touring",
            "2003 Volkswagen Passat W8 Variant 6-Speed",
            "2010 Volkswagen Jetta SportWagen TDI",
            "2023 Porsche Taycan Cross Turismo Turbo S",
            "1972 BMW 2002 Touring",
            "1991 Mercedes-Benz 230TE",
            "2006 Audi Allroad 2.7T 6-Speed",
        ],
        "nomatch": [
            "1969 Chevrolet Chevelle SS 396 4-Speed",
            "2014 BMW 535i Gran Turismo",
            "1998 Mercedes-Benz E320 Sedan",
            "2016 BMW M3 Sedan",
            "1991 Volvo 240 Wagon",
            "1998 Saab 9-5 SportCombi",
            "2015 Audi A6 3.0T Sedan",
            "1965 Ford Country Squire Wagon",
            "2002 BMW 530i Sedan",
            "1988 Mercedes-Benz 300E Sedan",
            "2019 Porsche Panamera 4S",
            "1973 BMW 3.0CS Coupe",
        ],
    },
}


@pytest.mark.parametrize("cid,title", [
    (cid, t) for cid, c in _CASES.items() for t in c["match"]
])
def test_category_should_match(cid, title):
    assert cid in categories.match_categories(_rec_from_title(title)), \
        f"{title!r} should match {cid}"


@pytest.mark.parametrize("cid,title", [
    (cid, t) for cid, c in _CASES.items() for t in c["nomatch"]
])
def test_category_should_not_match(cid, title):
    assert cid not in categories.match_categories(_rec_from_title(title)), \
        f"{title!r} should NOT match {cid}"


def test_engine_swap_not_mistaken_for_truck():
    # Honda K20 engine swap into a Skyline must NOT read as a Chevy K20 truck...
    skyline = _rec_from_title("K20-Powered 1983 Nissan Skyline RS-X Turbo 6-Speed")
    cats = categories.match_categories(skyline)
    assert "vintage-trucks" not in cats
    assert "80s-90s-japanese" in cats
    # ...but a real Chevy K10 (even LS-swapped) still counts as a truck.
    truck = _rec_from_title("LS-Powered 1972 Chevrolet K10 Stepside Pickup")
    assert "vintage-trucks" in categories.match_categories(truck)


def test_categories_are_liveness_agnostic():
    # categories.py does not know about auction status; the orchestrator filters
    # ended auctions before categorizing. A parsed ended-but-air-cooled record
    # still matches the predicate.
    html = (FIXTURES / "bat_auctions.html").read_text(encoding="utf-8")
    ended = next(parse.parse_item(r) for r in parse.parse_auctions_html(html) if r["id"] == 200000030)
    assert categories.match_categories(ended) == [AIRCOOLED]
