"""Validation tests: errors block the write, warnings are allowed."""

from scraper import validate
from scraper.write_snapshot import build_snapshot


def _auction(iid, *, amount=50000, currency="USD", status="live",
             url=None, category_ids=("air-cooled-911-family",), comments=10):
    return {
        "id": iid,
        "title": f"car {iid}",
        "year": 1985,
        "make": {"id": None, "name": "Porsche", "slug": "porsche"},
        "models": [{"id": None, "name": "911", "slug": "911"}],
        "taxonomy_paths": ["porsche/911"],
        "category_ids": list(category_ids),
        "bid": {"amount": amount, "currency": currency, "status": status},
        "engagement": {"comments": comments, "views": 100, "watchers": 20},
        "started_at": None,
        "ends_at": "2100-01-01T00:00:00Z",
        "flags": {"no_reserve": False, "premium": False, "alumni": None},
        "listing_url": url or f"https://bringatrailer.com/listing/car-{iid}/",
        "thumbnail_url": "https://bringatrailer.com/wp-content/uploads/x.jpg",
    }


def _snapshot(auctions, **kw):
    return build_snapshot(
        auctions,
        reported_live_count=kw.get("reported_live_count", len(auctions)),
        parsed_live_count=len(auctions),
        enriched_count=kw.get("enriched_count", len(auctions)),
    )


GOOD_METRICS = {
    "reported_live_count": 100,
    "parsed_ok_count": 100,
    "matched_count": 2,
    "enrich_attempted": 2,
    "enriched_count": 2,
    "enrichment_available": True,
}


def test_valid_snapshot_passes():
    snap = _snapshot([_auction(1), _auction(2)])
    errors, warnings = validate.validate_snapshot(snap, GOOD_METRICS)
    assert errors == []
    assert warnings == []


def test_empty_dataset_is_error():
    snap = _snapshot([])
    errors, _ = validate.validate_snapshot(snap, GOOD_METRICS)
    assert any("Empty dataset" in e for e in errors)


def test_duplicate_ids_is_error():
    snap = _snapshot([_auction(1), _auction(1)])
    errors, _ = validate.validate_snapshot(snap, GOOD_METRICS)
    assert any("Duplicate ids" in e for e in errors)


def test_invalid_url_is_error():
    snap = _snapshot([_auction(1, url="https://example.com/listing/x/"), _auction(2)])
    errors, _ = validate.validate_snapshot(snap, GOOD_METRICS)
    assert any("Invalid listing urls" in e for e in errors)


def test_non_http_url_is_error():
    snap = _snapshot([_auction(1, url="ftp://bringatrailer.com/x"), _auction(2)])
    errors, _ = validate.validate_snapshot(snap, GOOD_METRICS)
    assert any("Invalid listing urls" in e for e in errors)


def test_low_parse_coverage_is_error():
    snap = _snapshot([_auction(1), _auction(2)])
    metrics = dict(GOOD_METRICS, reported_live_count=100, parsed_ok_count=90)  # 90%
    errors, _ = validate.validate_snapshot(snap, metrics)
    assert any("Parsed coverage" in e for e in errors)


def test_parse_coverage_at_threshold_passes():
    snap = _snapshot([_auction(1), _auction(2)])
    metrics = dict(GOOD_METRICS, reported_live_count=100, parsed_ok_count=98)  # exactly 98%
    errors, _ = validate.validate_snapshot(snap, metrics)
    assert errors == []


def test_low_enrichment_coverage_is_warning_not_error():
    snap = _snapshot([_auction(1), _auction(2)])
    metrics = dict(GOOD_METRICS, enrich_attempted=10, enriched_count=5)  # 50%
    errors, warnings = validate.validate_snapshot(snap, metrics)
    assert errors == []
    assert any("enrichment coverage" in w for w in warnings)


def test_enrichment_coverage_skipped_when_unavailable():
    snap = _snapshot([_auction(1), _auction(2)])
    metrics = dict(GOOD_METRICS, enrichment_available=False, enrich_attempted=0, enriched_count=0)
    errors, warnings = validate.validate_snapshot(snap, metrics)
    assert errors == []
    assert not any("enrichment coverage" in w for w in warnings)


def test_unknown_currency_is_warning():
    snap = _snapshot([_auction(1, currency="XYZ"), _auction(2)])
    errors, warnings = validate.validate_snapshot(snap, GOOD_METRICS)
    assert errors == []
    assert any("currency" in w for w in warnings)


def test_bad_bid_amount_type_is_error():
    snap = _snapshot([_auction(1, amount="50000"), _auction(2)])  # string amount
    errors, _ = validate.validate_snapshot(snap, GOOD_METRICS)
    assert any("bid shape" in e.lower() for e in errors)


def test_missing_id_is_warning_not_error():
    a2 = _auction(2)
    a2["id"] = None
    snap = _snapshot([_auction(1), a2])
    errors, warnings = validate.validate_snapshot(snap, GOOD_METRICS)
    assert errors == []
    assert any("no id" in w for w in warnings)


def test_multiple_missing_ids_not_a_duplicate_error():
    a1, a2 = _auction(1), _auction(2)
    a1["id"] = None
    a2["id"] = None
    snap = _snapshot([a1, a2])
    errors, _ = validate.validate_snapshot(snap, GOOD_METRICS)
    assert not any("Duplicate ids" in e for e in errors)


def test_null_live_amount_is_warning():
    snap = _snapshot([_auction(1, amount=None, status="live"), _auction(2)])
    errors, warnings = validate.validate_snapshot(snap, GOOD_METRICS)
    assert errors == []
    assert any("null bid amount" in w for w in warnings)


# --- optional normalized blocks (Stage 1): vehicle_identity + analysis ---------------------
# Absence is valid; a malformed block is a soft WARNING and never blocks the write.

def test_absent_optional_blocks_are_valid():
    # the default _auction carries neither vehicle_identity nor analysis
    snap = _snapshot([_auction(1), _auction(2)])
    errors, warnings = validate.validate_snapshot(snap, GOOD_METRICS)
    assert errors == []
    assert not any("vehicle_identity" in w for w in warnings)
    assert not any("analysis" in w for w in warnings)


def test_wellformed_optional_blocks_pass_clean():
    a = _auction(1)
    a["vehicle_identity"] = {"year": 1990, "make": {"slug": "porsche"}, "model": {"slug": "911"}}
    a["analysis"] = {"score": None, "confidence": "low"}      # null score is valid, not malformed
    snap = _snapshot([a, _auction(2)])
    errors, warnings = validate.validate_snapshot(snap, GOOD_METRICS)
    assert errors == []
    assert not any("vehicle_identity" in w for w in warnings)
    assert not any("analysis" in w for w in warnings)


def test_malformed_vehicle_identity_is_warning_not_error():
    a = _auction(1)
    a["vehicle_identity"] = "not-a-dict"
    snap = _snapshot([a, _auction(2)])
    errors, warnings = validate.validate_snapshot(snap, GOOD_METRICS)
    assert errors == []
    assert any("vehicle_identity" in w for w in warnings)


def test_malformed_vehicle_identity_year_type_is_warning():
    a = _auction(1)
    a["vehicle_identity"] = {"year": "nineteen-ninety"}       # wrong type, must be int/None
    snap = _snapshot([a, _auction(2)])
    errors, warnings = validate.validate_snapshot(snap, GOOD_METRICS)
    assert errors == []
    assert any("vehicle_identity" in w for w in warnings)


def test_malformed_analysis_is_warning_not_error():
    a = _auction(1)
    a["analysis"] = ["not", "a", "dict"]
    snap = _snapshot([a, _auction(2)])
    errors, warnings = validate.validate_snapshot(snap, GOOD_METRICS)
    assert errors == []
    assert any("analysis" in w for w in warnings)


def test_malformed_analysis_score_type_is_warning():
    a = _auction(1)
    a["analysis"] = {"score": "0.5"}                          # non-numeric score is malformed
    snap = _snapshot([a, _auction(2)])
    errors, warnings = validate.validate_snapshot(snap, GOOD_METRICS)
    assert errors == []
    assert any("analysis" in w for w in warnings)
