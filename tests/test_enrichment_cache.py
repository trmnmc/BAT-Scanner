"""Tests for the cross-run enrichment cache."""

import json

import pytest

from scraper import enrichment_cache as ec


def _prev_snapshot(scraped_at="2026-06-20T00:00:00Z", auctions=None):
    return {"schema_version": 1, "scraped_at": scraped_at, "source": {}, "auctions": auctions or []}


def _rec(id_, url, **over):
    r = {
        "id": id_,
        "title": "current title",
        "listing_url": url,
        "bid": {"amount": 100, "currency": "USD", "status": "live"},
        "ends_at": "2026-06-23T00:00:00Z",
        "flags": {"no_reserve": True},
        "engagement": {"comments": None, "views": None, "watchers": None},
        "details": None,
        "value": None,
    }
    r.update(over)
    return r


# --- load_prev_snapshot -----------------------------------------------------

def test_load_missing_is_silent(tmp_path):
    snap, warn = ec.load_prev_snapshot(str(tmp_path / "nope.json"))
    assert snap is None and warn is None  # first run: not a warning


def test_load_malformed_warns(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    snap, warn = ec.load_prev_snapshot(str(p))
    assert snap is None and warn is not None


def test_load_wrong_shape_warns(tmp_path):
    p = tmp_path / "weird.json"
    p.write_text(json.dumps({"hello": "world"}))
    snap, warn = ec.load_prev_snapshot(str(p))
    assert snap is None and "shape" in warn


def test_load_good(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps(_prev_snapshot()))
    snap, warn = ec.load_prev_snapshot(str(p))
    assert warn is None and isinstance(snap, dict) and snap["auctions"] == []


# --- carry_forward_enrichment ----------------------------------------------

def test_carries_engagement_and_details_on_id_and_url_match():
    prev = _prev_snapshot(auctions=[{
        "id": 1, "listing_url": "https://bringatrailer.com/listing/x/",
        "engagement": {"comments": 12, "views": None, "watchers": 88},
        "details": {"miles": 42000, "condition": ["repaint"]},
        "enrichment": {"engagement_updated_at": "2026-06-19T10:00:00Z",
                       "details_updated_at": "2026-06-19T10:00:00Z"},
    }])
    cur = _rec(1, "https://bringatrailer.com/listing/x/")
    stats = ec.carry_forward_enrichment([cur], prev)
    assert stats["matched"] == 1
    assert cur["engagement"]["comments"] == 12 and cur["engagement"]["watchers"] == 88
    assert cur["details"]["miles"] == 42000
    assert cur["enrichment"]["engagement_updated_at"] == "2026-06-19T10:00:00Z"


def test_does_not_carry_volatile_board_fields():
    prev = _prev_snapshot(auctions=[{
        "id": 1, "listing_url": "https://bringatrailer.com/listing/x/",
        "title": "OLD title", "bid": {"amount": 999999, "currency": "EUR", "status": "live"},
        "ends_at": "2000-01-01T00:00:00Z", "flags": {"no_reserve": False},
        "value": {"fair_value": 1, "basis": "make-model-y3", "deal_pct": 0.9, "is_deal": True},
        "engagement": {"comments": 5, "watchers": 5},
    }])
    cur = _rec(1, "https://bringatrailer.com/listing/x/")
    ec.carry_forward_enrichment([cur], prev)
    # volatile fields stay as the CURRENT board values
    assert cur["title"] == "current title"
    assert cur["bid"]["amount"] == 100 and cur["bid"]["currency"] == "USD"
    assert cur["ends_at"] == "2026-06-23T00:00:00Z"
    assert cur["flags"]["no_reserve"] is True
    assert cur["value"] is None  # value is recomputed, never carried


def test_url_mismatch_does_not_carry():
    prev = _prev_snapshot(auctions=[{
        "id": 1, "listing_url": "https://bringatrailer.com/listing/OLD/",
        "engagement": {"comments": 9, "watchers": 9},
    }])
    cur = _rec(1, "https://bringatrailer.com/listing/NEW/")  # same id, different listing
    stats = ec.carry_forward_enrichment([cur], prev)
    assert stats["matched"] == 0
    assert cur["engagement"]["comments"] is None


def test_legacy_record_without_timestamps_falls_back_to_prev_scraped_at():
    prev = _prev_snapshot(scraped_at="2026-06-18T12:00:00Z", auctions=[{
        "id": 2, "listing_url": "https://bringatrailer.com/listing/y/",
        "engagement": {"comments": 3, "watchers": 7},
        "details": {"miles": 10000, "condition": []},
        # no "enrichment" block at all (legacy)
    }])
    cur = _rec(2, "https://bringatrailer.com/listing/y/")
    ec.carry_forward_enrichment([cur], prev)
    assert cur["enrichment"]["engagement_updated_at"] == "2026-06-18T12:00:00Z"
    assert cur["enrichment"]["details_updated_at"] == "2026-06-18T12:00:00Z"


def test_none_or_empty_prev_is_noop():
    cur = _rec(1, "https://bringatrailer.com/listing/x/")
    assert ec.carry_forward_enrichment([cur], None)["matched"] == 0
    assert ec.carry_forward_enrichment([cur], _prev_snapshot(auctions=[]))["prev_records"] == 0
    assert cur["engagement"]["comments"] is None  # untouched


def test_failed_refresh_retains_cached_data():
    # carry forward, then simulate a run where this listing was NOT refreshed: data persists.
    prev = _prev_snapshot(auctions=[{
        "id": 5, "listing_url": "https://bringatrailer.com/listing/z/",
        "engagement": {"comments": 20, "watchers": 50},
        "details": {"miles": 5000, "condition": ["numbers-matching"]},
        "enrichment": {"engagement_updated_at": "2026-06-19T00:00:00Z",
                       "details_updated_at": "2026-06-19T00:00:00Z"},
    }])
    cur = _rec(5, "https://bringatrailer.com/listing/z/")
    ec.carry_forward_enrichment([cur], prev)
    # no refresh happens -> still has the cached values
    assert cur["engagement"]["comments"] == 20
    assert cur["details"]["condition"] == ["numbers-matching"]


def test_stamp_enrichment_updates_only_fetched_portions():
    rec = _rec(1, "https://bringatrailer.com/listing/x/",
               enrichment={"engagement_updated_at": "old", "details_updated_at": "old"})
    ec.stamp_enrichment(rec, now_iso="NEW", engagement=True, details=False)
    assert rec["enrichment"]["engagement_updated_at"] == "NEW"
    assert rec["enrichment"]["details_updated_at"] == "old"  # not refreshed -> unchanged

    rec2 = _rec(2, "https://bringatrailer.com/listing/y/")  # no enrichment block yet
    ec.stamp_enrichment(rec2, now_iso="T", engagement=True, details=True)
    assert rec2["enrichment"]["engagement_updated_at"] == "T"
    assert rec2["enrichment"]["details_updated_at"] == "T"
