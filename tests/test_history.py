"""Auction history tests (no network). Covers the nine required cases plus idempotency,
round-trip, compaction, and the velocity helper.

Run:  python -m pytest tests/test_history.py
"""

import datetime as dt
import os
import tempfile

from scraper import history


def iso(unix):
    return dt.datetime.fromtimestamp(unix, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


# --- tiny record/snapshot builders -------------------------------------------------------------

def rec(id, *, bid=None, status="live", comments=None, watchers=None,
        ends_at="2026-06-25T00:00:00Z", no_reserve=False, url=None):
    return {
        "id": id,
        "listing_url": url or f"https://bringatrailer.com/listing/{id}/",
        "bid": {"amount": bid, "currency": "USD", "status": status},
        "engagement": {"comments": comments, "views": None, "watchers": watchers},
        "ends_at": ends_at,
        "flags": {"no_reserve": no_reserve, "premium": False, "alumni": None},
    }


def snap(records, scraped_at="2026-06-25T13:00:00Z"):
    return {"schema_version": 1, "scraped_at": scraped_at, "auctions": records}


OBS = "2026-06-25T13:00:00Z"
SRC = "snapshot:2026-06-25T13:00:00Z"


def diff(prev, curr, observed_at=OBS, source=SRC):
    return history.diff_records(prev, curr, observed_at=observed_at, source=source)


def types(events):
    return sorted(e["event_type"] for e in events)


def one(events, etype):
    matches = [e for e in events if e["event_type"] == etype]
    assert len(matches) == 1, f"expected exactly one {etype}, got {types(events)}"
    return matches[0]


# --- required: unchanged snapshot creates no (duplicate) event ---------------------------------

def test_unchanged_snapshot_creates_no_event():
    prev = [rec(1, bid=1000, comments=5, watchers=10)]
    curr = [rec(1, bid=1000, comments=5, watchers=10)]
    assert diff(prev, curr) == []


def test_reprocessing_same_snapshot_adds_no_duplicates():
    prev = snap([rec(1, bid=1000)], scraped_at="2026-06-24T13:00:00Z")
    curr = snap([rec(1, bid=2000)], scraped_at="2026-06-25T13:00:00Z")
    h = history.empty_history()
    first = history.record_from_snapshots(h, prev, curr, now=1_750_000_000)
    assert first["added"] == 1                       # one bid_changed
    # feeding the identical current snapshot again must not duplicate (observed_at is frozen)
    second = history.record_from_snapshots(h, prev, curr, now=1_750_000_000)
    assert second["added"] == 0
    assert len(h["events"]) == 1


# --- required: each change type ----------------------------------------------------------------

def test_bid_changed():
    e = one(diff([rec(1, bid=1000)], [rec(1, bid=1500)]), "bid_changed")
    assert e["previous"] == 1000 and e["current"] == 1500
    assert e["auction_key"] == "bat:1" and e["observed_at"] == OBS and e["source"] == SRC
    assert e["event_version"] == history.EVENT_VERSION


def test_comments_changed():
    e = one(diff([rec(1, bid=1000, comments=3)], [rec(1, bid=1000, comments=8)]), "comments_changed")
    assert e["previous"] == 3 and e["current"] == 8


def test_watchers_changed():
    e = one(diff([rec(1, bid=1000, watchers=20)], [rec(1, bid=1000, watchers=42)]), "watchers_changed")
    assert e["previous"] == 20 and e["current"] == 42


def test_end_time_extended():
    prev = [rec(1, bid=1000, ends_at="2026-06-25T00:00:00Z")]
    curr = [rec(1, bid=1000, ends_at="2026-06-25T02:00:00Z")]   # extended by 2h
    e = one(diff(prev, curr), "end_time_changed")
    assert e["previous"] == "2026-06-25T00:00:00Z" and e["current"] == "2026-06-25T02:00:00Z"


def test_reserve_status_changed():
    e = one(diff([rec(1, bid=1000, no_reserve=False)], [rec(1, bid=1000, no_reserve=True)]),
            "reserve_status_changed")
    assert e["previous"] is False and e["current"] is True


def test_sold():
    prev = [rec(1, bid=40000, status="live")]
    curr = [rec(1, bid=70000, status="sold")]
    ev = diff(prev, curr)
    e = one(ev, "sold")
    assert e["previous"] == 40000 and e["current"] == 70000
    # a terminal record does not also emit a bid_changed for the same close
    assert "bid_changed" not in types(ev)


def test_reserve_not_met():
    prev = [rec(1, bid=90000, status="live")]
    curr = [rec(1, bid=99000, status="reserve_not_met")]
    e = one(diff(prev, curr), "reserve_not_met")
    assert e["previous"] == 90000 and e["current"] == 99000


# --- required: invalid snapshot must not change history ----------------------------------------

def test_invalid_snapshot_leaves_history_untouched():
    prev = snap([rec(1, bid=1000)])
    curr = snap([rec(1, bid=9999)], scraped_at="2026-06-25T13:00:00Z")
    h = history.empty_history()
    history.append_events(h, [history._make_event("bat:0", "auction_seen", None, {},
                                                  observed_at="2026-01-01T00:00:00Z", source="x")])
    before = list(h["events"])
    out = history.record_from_snapshots(h, prev, curr, valid=False, now=1_750_000_000)
    assert out["added"] == 0
    assert h["events"] == before          # nothing added, nothing dropped


# --- required: fewer than two observations gives null velocity ---------------------------------

def test_velocity_null_with_fewer_than_two_observations():
    h = history.empty_history()
    history.append_events(h, [history._make_event(
        "bat:1", "bid_changed", None, 1000, observed_at="2026-06-24T13:00:00Z", source="x")])
    assert history.daily_movement(h, "bat:1", "bid") is None


def test_daily_movement_two_observations_shows_window():
    h = history.empty_history()
    history.append_events(h, [
        history._make_event("bat:1", "auction_seen", None,
                            {"bid": 1000, "comments": None, "watchers": None, "ends_at": None},
                            observed_at="2026-06-23T13:00:00Z", source="x"),
        history._make_event("bat:1", "bid_changed", 1000, 4000,
                            observed_at="2026-06-25T13:00:00Z", source="y"),
    ])
    mv = history.daily_movement(h, "bat:1", "bid")
    assert mv is not None
    assert mv["observations"] == 2
    assert mv["change"] == 3000
    assert mv["window_days"] == 2.0
    assert mv["per_day"] == 1500.0
    assert mv["label"] == "daily movement"
    assert mv["first_observed_at"] == "2026-06-23T13:00:00Z"
    assert mv["last_observed_at"] == "2026-06-25T13:00:00Z"


# --- churn: new + gone -------------------------------------------------------------------------

def test_auction_seen_and_listing_ended():
    prev = [rec(1, bid=1000)]
    curr = [rec(2, bid=500)]
    ev = diff(prev, curr)
    assert types(ev) == ["auction_seen", "listing_ended"]
    seen = one(ev, "auction_seen")
    assert seen["previous"] is None and seen["current"]["bid"] == 500
    ended = one(ev, "listing_ended")
    assert ended["auction_key"] == "bat:1" and ended["previous"] == 1000 and ended["current"] is None


def test_brand_new_terminal_record_emits_nothing():
    # a car never seen live that is already terminal (in ended_records but not the prior snapshot):
    # prev is None, so there is no live->terminal transition to record. It must not crash and must
    # emit NOTHING (emitting it would re-fire every run it lingers, each with previous=None).
    assert diff([], [rec(7, bid=12000, status="sold")]) == []


def test_lingering_terminal_record_does_not_re_emit():
    # Run B: the car was live last run (prev), now terminal on the board -> sold ONCE, with the
    # real prior bid as previous.
    h = history.empty_history()
    r1 = history.record_observation(h, [rec(1, bid=1000, status="live")],
                                    [rec(1, bid=9000, status="sold")],
                                    observed_at="2026-06-25T13:00:00Z", source="s1", now=1_750_000_000)
    assert r1["added"] == 1 and types(r1["events"]) == ["sold"]
    assert one(r1["events"], "sold")["previous"] == 1000
    # Run C: the snapshot stored only live cars, so prev no longer contains the sold car, yet it
    # still lingers on the fetched board. It must NOT re-emit.
    r2 = history.record_observation(h, [], [rec(1, bid=9000, status="sold")],
                                    observed_at="2026-06-26T13:00:00Z", source="s2", now=1_750_086_400)
    assert r2["added"] == 0


def test_recycled_id_on_new_url_is_end_plus_seen():
    prev = [rec(1, bid=1000, url="https://bringatrailer.com/listing/old/")]
    curr = [rec(1, bid=200, url="https://bringatrailer.com/listing/new/")]
    ev = diff(prev, curr)
    assert types(ev) == ["auction_seen", "listing_ended"]


# --- never treat missing as zero ---------------------------------------------------------------

def test_missing_current_bid_is_not_a_change():
    # previous had a bid, current is unknown (None) -> NOT recorded as a drop to 0.
    assert diff([rec(1, bid=1000)], [rec(1, bid=None)]) == []


def test_first_real_bid_records_previous_null_not_zero():
    e = one(diff([rec(1, bid=None)], [rec(1, bid=1500)]), "bid_changed")
    assert e["previous"] is None and e["current"] == 1500


# --- compaction / retention --------------------------------------------------------------------

def test_compaction_drops_events_older_than_retention():
    h = history.empty_history()
    h["events"] = [
        history._make_event("bat:1", "bid_changed", 1, 2,
                            observed_at="2020-01-01T00:00:00Z", source="old"),   # ancient
        history._make_event("bat:1", "bid_changed", 2, 3,
                            observed_at="2026-06-25T00:00:00Z", source="new"),
    ]
    now = 1_782_000_000  # well after 2026-06-25
    removed = history.compact_history(h, now=now)
    assert removed == 1
    assert len(h["events"]) == 1 and h["events"][0]["source"] == "new"


def test_compaction_caps_events_per_auction():
    h = history.empty_history()
    base = 1_700_000_000
    h["events"] = [
        history._make_event("bat:1", "bid_changed", i, i + 1,
                            observed_at=iso(base + i * 86400), source=str(i))
        for i in range(10)
    ]
    history.compact_history(h, now=base + 11 * 86400,
                            retention_seconds=10_000 * 86400, max_per_auction=3)
    kept = h["events"]
    assert len(kept) == 3
    # newest three retained
    assert [e["previous"] for e in sorted(kept, key=lambda e: e["observed_at"])] == [7, 8, 9]


# --- round-trip + atomic save ------------------------------------------------------------------

def test_save_and_load_round_trip():
    h = history.empty_history()
    history.append_events(h, diff([rec(1, bid=1000)], [rec(1, bid=2000)]))
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "history.json")
        history.save_history(h, path, generated_at="2026-06-25T13:00:00Z")
        assert os.path.exists(path)
        back = history.load_history(path)
        assert len(back["events"]) == 1
        assert back["events"][0]["event_type"] == "bid_changed"
        assert back["schema_version"] == history.HISTORY_SCHEMA_VERSION


def test_load_missing_file_is_empty_not_error():
    with tempfile.TemporaryDirectory() as d:
        h = history.load_history(os.path.join(d, "nope.json"))
        assert h["events"] == []
