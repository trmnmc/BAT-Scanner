"""Carry enrichment forward between scraper runs.

The live board only ever carries null engagement; per-listing enrichment (comments,
watchers, mileage, condition) is fetched for a capped subset each run. Without a cache,
anything not re-fetched this run would lose its previously-fetched data. This module loads
the previous snapshot and copies that data onto the matching current records BEFORE value
scoring, so enrichment persists and accumulates across runs.

It carries ONLY enrichment:
  - engagement   (comments / views / watchers)
  - details      (mileage / condition)
  - enrichment   (the engagement_updated_at / details_updated_at timestamps)

It never carries volatile, current-board fields (bid, ends_at, flags, value, title, ...).
A record is matched only when BOTH id and listing_url agree, so a recycled id on a
different listing can't drag stale data onto the wrong car.
"""

from __future__ import annotations

import json
import os


def _meaningful_engagement(eng) -> bool:
    return bool(eng) and (eng.get("comments") is not None or eng.get("watchers") is not None)


def _usable_details(det) -> bool:
    return bool(det) and (det.get("miles") is not None or bool(det.get("condition")))


def load_prev_snapshot(path):
    """Return (snapshot_dict_or_None, warning_or_None).

    Missing file -> (None, None): a first run has no cache, which is normal, not a warning.
    Present but unreadable / not JSON / wrong shape -> (None, "<reason>"): the caller warns
    and continues without a cache (never aborts the scrape).
    """
    if not path or not os.path.exists(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            snap = json.load(fh)
    except (OSError, ValueError) as e:
        return None, f"previous snapshot at {path} could not be read for the enrichment cache: {e}"
    if not isinstance(snap, dict) or not isinstance(snap.get("auctions"), list):
        return None, f"previous snapshot at {path} has an unexpected shape; enrichment cache skipped"
    return snap, None


def stamp_enrichment(record, *, now_iso, engagement=False, details=False) -> None:
    """Stamp the auction-level enrichment block after a successful per-listing fetch.

    Only stamps the portion(s) that actually parsed this run; the other portion keeps its
    prior timestamp (set by carry_forward). Stored at the auction level so we never have to
    change parse_listing_engagement / parse_listing_details return contracts.
    """
    block = record.get("enrichment")
    if not isinstance(block, dict):
        block = {"engagement_updated_at": None, "details_updated_at": None}
    if engagement:
        block["engagement_updated_at"] = now_iso
    if details:
        block["details_updated_at"] = now_iso
    record["enrichment"] = block


def carry_forward_enrichment(records, prev_snapshot) -> dict:
    """Copy cached enrichment from prev_snapshot onto matching current `records` (in place).

    Returns stats: {prev_records, matched, carried_engagement, carried_details}. A None or
    empty prev_snapshot is a no-op (returns zeroed stats). Records keep an `enrichment` block
    only when something was carried; legacy cached data without timestamps falls back to the
    previous snapshot's scraped_at.
    """
    stats = {"prev_records": 0, "matched": 0, "carried_engagement": 0, "carried_details": 0}
    if not isinstance(prev_snapshot, dict):
        return stats
    prev_list = prev_snapshot.get("auctions")
    if not isinstance(prev_list, list) or not prev_list:
        return stats
    prev_scraped_at = prev_snapshot.get("scraped_at")

    # index previous records by id (only those with an id and a listing_url to match on)
    prev_by_id = {}
    for p in prev_list:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if pid is None or not p.get("listing_url"):
            continue
        prev_by_id[pid] = p
    stats["prev_records"] = len(prev_by_id)

    for cur in records:
        pid = cur.get("id")
        if pid is None:
            continue
        p = prev_by_id.get(pid)
        if p is None or p.get("listing_url") != cur.get("listing_url"):
            continue  # id+url must both agree
        stats["matched"] += 1

        prev_enr = p.get("enrichment") if isinstance(p.get("enrichment"), dict) else {}
        eng_ts = prev_enr.get("engagement_updated_at")
        det_ts = prev_enr.get("details_updated_at")

        carried_eng = carried_det = False
        prev_eng = p.get("engagement")
        if _meaningful_engagement(prev_eng):
            cur["engagement"] = dict(prev_eng)
            carried_eng = True
            stats["carried_engagement"] += 1
            if eng_ts is None:
                eng_ts = prev_scraped_at  # legacy fallback: stamp with the prior scan time

        prev_det = p.get("details")
        if _usable_details(prev_det):
            cur["details"] = dict(prev_det)
            carried_det = True
            stats["carried_details"] += 1
            if det_ts is None:
                det_ts = prev_scraped_at  # legacy fallback

        if carried_eng or carried_det or prev_enr:
            cur["enrichment"] = {
                "engagement_updated_at": eng_ts if carried_eng else (eng_ts if prev_enr else None),
                "details_updated_at": det_ts if carried_det else (det_ts if prev_enr else None),
            }
    return stats
