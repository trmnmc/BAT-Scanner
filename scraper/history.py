"""Auction history events — record useful *changes* between runs, not full snapshots.

Stage 9. The goal is to begin keeping a small, bounded log of what actually changed on the
live board from one valid snapshot to the next — without building a database and without
storing a duplicate copy of the whole board every run.

How it works
------------
Each run we compare the PREVIOUS valid auction state to the NEW valid auction state and emit
an event only when something important changed (a new listing, a bid move, an engagement move,
an end-time extension, a reserve-flag flip, or the listing leaving the live board). Nothing is
written when nothing changed, so an idle board adds nothing.

Idempotency (so reprocessing the same snapshot can't duplicate events)
----------------------------------------------------------------------
`observed_at` and `source` are taken from the CURRENT snapshot's frozen ``scraped_at`` — never
from the wall clock. Two events are "the same" when their (auction_key, event_type, observed_at,
previous, current) match, so feeding the identical snapshot twice produces the identical events
and they collapse on append. A genuinely new run has a new ``scraped_at`` and is a new
observation, as intended.

Honest limitations (we do NOT claim real-time history)
------------------------------------------------------
The live board carries only LIVE auctions, so a car that finishes usually just disappears from
the board between runs: that is recorded as ``listing_ended`` (outcome unknown). ``sold`` and
``reserve_not_met`` are only emitted when a record still on the fetched board has flipped to that
terminal status, which is comparatively rare. The event *types* and detection logic exist and are
tested; their production frequency depends entirely on BaT's board timing.

Data structure (data/history.json) — a compact, append-friendly log
-------------------------------------------------------------------
    {
      "schema_version": 1,
      "generated_at": "2026-06-25T13:00:00Z",   // when this file was last written (may be null)
      "event_count": 1234,
      "events": [
        // one event per line; oldest first. Each event is exactly these fields:
        {"auction_key":"bat:115717336","current":45000,"event_type":"bid_changed",
         "event_version":1,"observed_at":"2026-06-25T13:00:00Z","previous":42000,
         "source":"snapshot:2026-06-25T13:00:00Z"}
      ]
    }

Event fields (task 3):
    event_version  schema version of the event itself (int)
    auction_key    "bat:<id>" — stable per listing
    observed_at    the current snapshot's scraped_at (ISO-8601 Z)
    event_type     one of EVENT_TYPES
    previous       the value before the change (may be null — never coerced to 0)
    current        the value after the change (may be null for listing_ended)
    source         "snapshot:<scraped_at>" — provenance + part of the idempotency key
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import time

EVENT_VERSION = 1
HISTORY_SCHEMA_VERSION = 1

# Retention / compaction (task 9): history is bounded so it never grows forever.
HISTORY_RETENTION_DAYS = 90
HISTORY_RETENTION_SECONDS = HISTORY_RETENTION_DAYS * 86400
# Safety cap per auction so a single pathological listing can't bloat the log. An auction runs
# ~7-30 days, so even daily bid moves stay well under this; it only guards against runaway cases.
MAX_EVENTS_PER_AUCTION = 400

EVENT_TYPES = frozenset({
    "auction_seen",
    "bid_changed",
    "comments_changed",
    "watchers_changed",
    "end_time_changed",
    "reserve_status_changed",
    "listing_ended",
    "sold",
    "reserve_not_met",
})

# bid.status values that mean the auction is over (no further change events for it this run).
_TERMINAL_STATUSES = {"sold", "reserve_not_met", "ended"}

# Which event types carry a numeric value for a given velocity metric. auction_seen carries a
# small baseline dict and is handled separately so velocity has a first data point.
_METRIC_EVENTS = {
    "bid": {"bid_changed", "sold", "reserve_not_met"},
    "comments": {"comments_changed"},
    "watchers": {"watchers_changed"},
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def auction_key_of(record) -> str | None:
    """Stable key for a record, "bat:<id>". None when the record has no id."""
    if not isinstance(record, dict):
        return None
    rid = record.get("id")
    return f"bat:{rid}" if rid is not None else None


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(s):
    """ISO-8601 (optionally Z) -> aware datetime, or None when unparseable/empty."""
    if not isinstance(s, str) or not s:
        return None
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# All accessors are None-safe: a brand-new terminal record has no `previous`, so `prev` can be
# None when we read its bid/engagement. A missing record (or block) reads as None, never 0.
def _bid_amount(record):
    bid = record.get("bid") if isinstance(record, dict) else None
    return bid.get("amount") if isinstance(bid, dict) else None


def _bid_status(record):
    bid = record.get("bid") if isinstance(record, dict) else None
    return bid.get("status") if isinstance(bid, dict) else None


def _eng(record, field):
    eng = record.get("engagement") if isinstance(record, dict) else None
    return eng.get(field) if isinstance(eng, dict) else None


def _no_reserve(record):
    flags = record.get("flags") if isinstance(record, dict) else None
    return flags.get("no_reserve") if isinstance(flags, dict) else None


def _make_event(auction_key, event_type, previous, current, *, observed_at, source) -> dict:
    return {
        "event_version": EVENT_VERSION,
        "auction_key": auction_key,
        "observed_at": observed_at,
        "event_type": event_type,
        "previous": previous,
        "current": current,
        "source": source,
    }


def _event_key(ev):
    """Identity tuple used to dedupe (task 6). Stable JSON for previous/current so dicts and
    numbers compare consistently."""
    return (
        ev.get("auction_key"),
        ev.get("event_type"),
        ev.get("observed_at"),
        json.dumps(ev.get("previous"), sort_keys=True, default=str),
        json.dumps(ev.get("current"), sort_keys=True, default=str),
    )


# ---------------------------------------------------------------------------
# diffing two valid auction states -> events
# ---------------------------------------------------------------------------

def _index_by_key(records):
    """auction_key -> record, keeping the LAST record for a key (caller passes live then ended,
    so a terminal record wins over a stale live duplicate of the same id)."""
    out = {}
    for r in records or []:
        if not isinstance(r, dict):
            continue
        key = auction_key_of(r)
        if key is None:
            continue
        out[key] = r
    return out


def _changed_field_events(key, prev, cur, *, observed_at, source):
    """Events for a still-live, matched auction: bid / comments / watchers / end-time / reserve.

    Critical invariant: a change is recorded only when `current` is known. `previous` may be
    null (was unknown) but is stored as null — never coerced to 0."""
    events = []

    # bid_changed — board carries current_bid for the whole board, so this is broadly available.
    pb, cb = _bid_amount(prev), _bid_amount(cur)
    if cb is not None and cb != pb:
        events.append(_make_event(key, "bid_changed", pb, cb, observed_at=observed_at, source=source))

    # comments_changed / watchers_changed (only where enrichment supplies the number)
    for field, etype in (("comments", "comments_changed"), ("watchers", "watchers_changed")):
        pv, cv = _eng(prev, field), _eng(cur, field)
        if cv is not None and cv != pv:
            events.append(_make_event(key, etype, pv, cv, observed_at=observed_at, source=source))

    # end_time_changed — BaT can extend a closing time; only compare when both are present.
    pe, ce = prev.get("ends_at"), cur.get("ends_at")
    if isinstance(pe, str) and isinstance(ce, str) and pe and ce and pe != ce:
        events.append(_make_event(key, "end_time_changed", pe, ce, observed_at=observed_at, source=source))

    # reserve_status_changed — the no_reserve flag flipped.
    pr, cr = _no_reserve(prev), _no_reserve(cur)
    if isinstance(pr, bool) and isinstance(cr, bool) and pr != cr:
        events.append(_make_event(key, "reserve_status_changed", pr, cr,
                                  observed_at=observed_at, source=source))
    return events


def _seen_baseline(record):
    """Compact first-observation baseline stored on auction_seen.current so velocity helpers have
    a starting point for each metric."""
    return {
        "bid": _bid_amount(record),
        "comments": _eng(record, "comments"),
        "watchers": _eng(record, "watchers"),
        "ends_at": record.get("ends_at"),
    }


def diff_records(prev_records, curr_records, *, observed_at, source):
    """Compare the previous valid auction state to the new one and return the list of events.

    Matching is by auction_key AND listing_url (a recycled id on a different listing is treated
    as the old one ending plus a new one appearing, never as a continuation). Output is sorted by
    (auction_key, event_type) for deterministic, diff-friendly files.
    """
    prev_by_key = _index_by_key(prev_records)
    curr_by_key = _index_by_key(curr_records)
    events = []

    for key in set(prev_by_key) | set(curr_by_key):
        prev = prev_by_key.get(key)
        cur = curr_by_key.get(key)

        if prev is not None and cur is not None and prev.get("listing_url") != cur.get("listing_url"):
            # same id, different listing -> old listing ended, new listing seen.
            events.append(_make_event(key, "listing_ended", _bid_amount(prev), None,
                                      observed_at=observed_at, source=source))
            prev = None  # fall through to the curr-only (auction_seen) handling below

        if cur is None:
            # left the live board entirely; outcome unknown -> listing_ended.
            events.append(_make_event(key, "listing_ended", _bid_amount(prev), None,
                                      observed_at=observed_at, source=source))
            continue

        status = _bid_status(cur)
        is_new = prev is None

        if status in _TERMINAL_STATUSES:
            # A terminal event is a live -> terminal TRANSITION, so we record it only when we have a
            # prior live state to transition from (prev present, with the real prior bid). A car we
            # never saw live (prev is None) is skipped: the snapshot stores only LIVE cars, so a
            # terminal record is never persisted as the next run's prev — emitting it would re-fire
            # every run it lingers on the board, each time with a meaningless previous=None. Skipping
            # keeps the log idempotent at the cost of not logging a car whose entire life we missed.
            if prev is not None:
                etype = ("sold" if status == "sold"
                         else "reserve_not_met" if status == "reserve_not_met"
                         else "listing_ended")   # "ended" — terminal, outcome not stated
                events.append(_make_event(key, etype, _bid_amount(prev), _bid_amount(cur),
                                          observed_at=observed_at, source=source))
            continue

        # still-live record
        if is_new:
            # auction_seen marks a LIVE auction we can now track.
            events.append(_make_event(key, "auction_seen", None, _seen_baseline(cur),
                                      observed_at=observed_at, source=source))
        else:
            events.extend(_changed_field_events(key, prev, cur, observed_at=observed_at, source=source))

    events.sort(key=lambda e: (e["auction_key"], e["event_type"]))
    return events


# ---------------------------------------------------------------------------
# history container: load / append (dedup) / compact / save
# ---------------------------------------------------------------------------

def empty_history() -> dict:
    return {"schema_version": HISTORY_SCHEMA_VERSION, "generated_at": None, "events": []}


def load_history(path) -> dict:
    """Load history, or an empty container when the file is missing/unreadable/misshapen.

    A missing file is normal (first run). A corrupt file degrades to empty rather than crashing
    the scrape — but see write semantics: we never overwrite a valid history on a failed run."""
    if not path or not os.path.exists(path):
        return empty_history()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return empty_history()
    if not isinstance(doc, dict) or not isinstance(doc.get("events"), list):
        return empty_history()
    doc.setdefault("schema_version", HISTORY_SCHEMA_VERSION)
    return doc


def append_events(history, events) -> int:
    """Append events to history, skipping any that duplicate one already present (task 6).
    Returns the number actually added."""
    existing = history.setdefault("events", [])
    seen = {_event_key(e) for e in existing}
    added = 0
    for ev in events or []:
        k = _event_key(ev)
        if k in seen:
            continue
        seen.add(k)
        existing.append(ev)
        added += 1
    return added


def compact_history(history, *, now=None, retention_seconds=HISTORY_RETENTION_SECONDS,
                    max_per_auction=MAX_EVENTS_PER_AUCTION) -> int:
    """Bound history in place (task 9). Drops events older than the retention window, then caps
    the number of events kept per auction (newest first). Returns how many events were removed.

    Events with an unparseable observed_at are kept (we never silently discard data we can't
    date), but they don't count toward velocity windows. The per-auction cap keeps the NEWEST
    events, so on a (practically unreachable at the default 400) overflow it can evict an
    auction's oldest events — including a metric's auction_seen baseline, which would shorten that
    metric's daily-movement window. The default cap is set far above any realistic auction so this
    only guards against a pathological runaway."""
    now = time.time() if now is None else now
    events = history.get("events") or []
    cutoff = now - retention_seconds

    kept = []
    for e in events:
        ts = _parse_iso(e.get("observed_at"))
        if ts is None or ts.timestamp() >= cutoff:
            kept.append(e)

    # per-auction cap: keep the newest max_per_auction by observed_at (undated sort last/oldest).
    if max_per_auction is not None:
        by_key = {}
        for e in kept:
            by_key.setdefault(e.get("auction_key"), []).append(e)
        over = set()
        for key, evs in by_key.items():
            if len(evs) <= max_per_auction:
                continue
            ordered = sorted(evs, key=lambda e: (_parse_iso(e.get("observed_at"))
                                                 or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)))
            for e in ordered[:-max_per_auction]:
                over.add(id(e))
        kept = [e for e in kept if id(e) not in over]

    removed = len(events) - len(kept)
    history["events"] = kept
    return removed


def save_history(history, path, *, generated_at=None) -> str:
    """Atomically write history (temp file + os.replace) as compact, one-event-per-line JSON.

    One event per line keeps git diffs readable (an idle-to-busy run shows as added lines) while
    staying far smaller than indented JSON. A crash never leaves a half-written history."""
    events = history.get("events") or []
    schema = history.get("schema_version", HISTORY_SCHEMA_VERSION)
    lines = ["{",
             f'  "schema_version": {json.dumps(schema)},',
             f'  "generated_at": {json.dumps(generated_at)},',
             f'  "event_count": {json.dumps(len(events))},']
    if events:
        body = ",\n".join(
            "    " + json.dumps(e, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for e in events)
        lines.append('  "events": [')
        lines.append(body)
        lines.append("  ]")
    else:
        lines.append('  "events": []')
    lines.append("}")
    text = "\n".join(lines) + "\n"

    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".history-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


# ---------------------------------------------------------------------------
# top-level: record one observation (the pipeline entry point)
# ---------------------------------------------------------------------------

def record_observation(history, prev_records, curr_records, *, observed_at, source,
                       valid=True, now=None) -> dict:
    """Diff prev->curr, append the new events (deduped), and compact. Returns
    {"history", "added", "events", "removed"}.

    Task 8: when `valid` is False (scraping or validation failed) this is a NO-OP — history is
    returned untouched so a bad run can never change or overwrite a valid history.
    """
    if not valid:
        return {"history": history, "added": 0, "events": [], "removed": 0}
    events = diff_records(prev_records, curr_records, observed_at=observed_at, source=source)
    added = append_events(history, events)
    removed = compact_history(history, now=now)
    return {"history": history, "added": added, "events": events, "removed": removed}


def record_from_snapshots(history, prev_snapshot, curr_snapshot, *, valid=True, now=None) -> dict:
    """Convenience wrapper that pulls records + provenance from snapshot dicts.

    `observed_at` and `source` come from the CURRENT snapshot's frozen scraped_at, which is what
    makes reprocessing the same snapshot idempotent.
    """
    if not valid:
        return {"history": history, "added": 0, "events": [], "removed": 0}
    prev_records = (prev_snapshot or {}).get("auctions") if isinstance(prev_snapshot, dict) else None
    curr_records = (curr_snapshot or {}).get("auctions") if isinstance(curr_snapshot, dict) else None
    scraped_at = (curr_snapshot or {}).get("scraped_at") if isinstance(curr_snapshot, dict) else None
    observed_at = scraped_at or _now_iso()
    source = f"snapshot:{scraped_at}" if scraped_at else "snapshot:unknown"
    return record_observation(history, prev_records, curr_records,
                              observed_at=observed_at, source=source, valid=True, now=now)


# ---------------------------------------------------------------------------
# velocity helpers (tasks 11-13): "daily movement", never "live velocity"
# ---------------------------------------------------------------------------

def _observations(history, auction_key, metric):
    """Collect (datetime, value) observations for one auction + metric, oldest first.

    Sources: the auction_seen baseline (current[metric]) plus every change event that carries a
    number for the metric. Points with a null value or an unparseable timestamp are skipped — a
    missing value is never treated as 0. Duplicate timestamps keep the last value."""
    wanted = _METRIC_EVENTS.get(metric, set())
    points = {}
    for e in history.get("events") or []:
        if e.get("auction_key") != auction_key:
            continue
        etype = e.get("event_type")
        val = None
        if etype == "auction_seen":
            cur = e.get("current")
            if isinstance(cur, dict):
                val = cur.get(metric)
        elif etype in wanted:
            val = e.get("current")
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            continue
        ts = _parse_iso(e.get("observed_at"))
        if ts is None:
            continue
        points[ts] = val
    return sorted(points.items())


def daily_movement(history, auction_key, metric="bid") -> dict | None:
    """Average daily movement of `metric` for one auction, or None.

    Returns None (task 12) unless there are at least two observations with valid timestamps and a
    positive time window. The result always shows the window it was measured over, and is labeled
    "daily movement" (task 13) — these are sparse daily observations, NOT a live, real-time rate.
    """
    obs = _observations(history, auction_key, metric)
    if len(obs) < 2:
        return None
    (t0, v0), (t1, v1) = obs[0], obs[-1]
    window_seconds = (t1 - t0).total_seconds()
    if window_seconds <= 0:
        return None
    window_days = window_seconds / 86400.0
    change = v1 - v0
    return {
        "metric": metric,
        "label": "daily movement",
        "observations": len(obs),
        "first_observed_at": t0.isoformat().replace("+00:00", "Z"),
        "last_observed_at": t1.isoformat().replace("+00:00", "Z"),
        "first_value": v0,
        "last_value": v1,
        "change": change,
        "window_seconds": window_seconds,
        "window_days": round(window_days, 4),
        "per_day": round(change / window_days, 4),
    }


# ---------------------------------------------------------------------------
# report support
# ---------------------------------------------------------------------------

def summarize(history, *, path=None) -> dict:
    """Aggregate stats for tools/history_report.py (task 10)."""
    events = history.get("events") or []
    by_type = {}
    keys = set()
    oldest = newest = None
    for e in events:
        by_type[e.get("event_type")] = by_type.get(e.get("event_type"), 0) + 1
        if e.get("auction_key") is not None:
            keys.add(e["auction_key"])
        oa = e.get("observed_at")
        if isinstance(oa, str) and oa:
            if oldest is None or oa < oldest:
                oldest = oa
            if newest is None or oa > newest:
                newest = oa
    file_bytes = None
    if path and os.path.exists(path):
        try:
            file_bytes = os.path.getsize(path)
        except OSError:
            file_bytes = None
    return {
        "total_events": len(events),
        "auctions_tracked": len(keys),
        "event_counts_by_type": dict(sorted(by_type.items())),
        "oldest_event": oldest,
        "newest_event": newest,
        "file_bytes": file_bytes,
    }
