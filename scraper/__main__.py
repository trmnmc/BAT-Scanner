"""Pipeline entry point.  Run with:  python -m scraper

  fetch -> parse -> filter-live -> categorize -> enrich(matched) -> validate -> write

The snapshot stores ALL live auctions (each tagged with category_ids), so future
categories work without re-scraping. Engagement enrichment is fetched only for the
matched-category records, to keep request volume low.

Flags:
  --offline            use fixtures/ instead of the network (also what tests exercise)
  --fixtures-dir DIR   fixtures location (default: ./fixtures)
  --out PATH           snapshot path (default: ./data/auctions.json)
  --no-enrich          skip engagement enrichment
  --max-enrich-pages N cap listings-filter pages fetched (default 25)
  --only CID[,CID]     restrict categorization to these category ids
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import time

from . import categories, comps, parse, value
from .fetch import (
    BlockedError,
    FetchError,
    fetch_auctions_html,
    fetch_listing_html,
    fetch_listings_filter_page,
    read_text,
)
from .validate import validate_snapshot
from .write_snapshot import build_snapshot, write_snapshot

_STALL_LIMIT = 3  # consecutive bulk pages with no new matched id -> stop


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _enrich_per_listing(matched, *, offline, fixtures_dir):
    """Reliable engagement: read each matched car's listing page.

    The matched set is small, so this is low volume (one request per car). This is
    the default for live runs because the bulk listings-filter endpoint does not
    surface live auctions in a pageable order (observed ~0% coverage).
    Returns ({id: engagement}, request_count).
    """
    fixture_html = None
    if offline:
        fixture_html = read_text(os.path.join(fixtures_dir, "bat_listing.html"))
    eng, details, requests = {}, {}, 0
    for rec in matched:
        try:
            html = fixture_html if offline else fetch_listing_html(rec["listing_url"])
            requests += 1
            eng[rec["id"]] = parse.parse_listing_engagement(html)
            # mileage + condition come free from the same already-fetched page
            details[rec["id"]] = parse.parse_listing_details(html, rec.get("title", ""))
        except (BlockedError, FetchError, OSError) as e:
            print(f"Note: could not enrich listing {rec.get('id')}: {e}", file=sys.stderr)
    return eng, details, requests


def _enrich_bulk(matched_ids, *, offline, fixtures_dir, min_year, max_year, max_pages):
    """Engagement via the listings-filter endpoint, joined by id.

    Offline: a single join against the fixture (deterministic). Live: page-and-collect
    with a stall guard and page cap. Live coverage is unreliable (kept for the fixture
    test and as an opt-in); per-listing enrichment is preferred for live.
    Returns ({id: engagement}, request_count).
    """
    if offline:
        text = read_text(os.path.join(fixtures_dir, "bat_listings_filter.json"))
        page_map = parse.parse_listings_filter(text)
        # bulk (listings-filter) carries no mileage/condition; details stay empty here
        return {i: page_map[i] for i in matched_ids if i in page_map}, {}, 1

    remaining = set(matched_ids)
    eng, pages_used, stalls = {}, 0, 0
    for page in range(1, max_pages + 1):
        if not remaining:
            break
        text = fetch_listings_filter_page(page, minimum_year=min_year, maximum_year=max_year)
        pages_used += 1
        page_map = parse.parse_listings_filter(text)
        new = 0
        for iid, e in page_map.items():
            if iid in remaining:
                eng[iid] = e
                remaining.discard(iid)
                new += 1
        if new == 0:
            stalls += 1
            if stalls >= _STALL_LIMIT:
                break
        else:
            stalls = 0
    return eng, {}, pages_used


# Bounded enrichment (pivot 2026-06-21): deal scoring is free from board price + comps, so we
# enrich only a capped high-value set for engagement/mileage/condition badges.
ENRICH_ENDS_SOON_SECONDS = 48 * 3600   # cars ending within 48h (the ticker / is_deal window)
ENRICH_DEAL_MARGIN = 0.15              # free deal_pct >= this => a deal candidate worth badges
ENRICH_CAP = 300                       # hard per-run cap on per-listing fetches (politeness)
ENRICH_SAMPLE_TARGET = 40              # ~rolling sample/run so coverage of the rest fills in


def _select_enrichment_targets(scored, now):
    """Bounded high-value set to enrich: ending-soon (soonest first) + deal candidates + a
    deterministic rolling sample of the rest, hard-capped at ENRICH_CAP. Keeps the BaT
    footprint low while putting mileage/condition badges where they matter."""
    soon, deal_cands, rest = [], [], []
    for r in scored:
        v = r.get("value") or {}
        ends = r.get("_ends_ts")
        is_soon = isinstance(ends, (int, float)) and 0 <= ends - now <= ENRICH_ENDS_SOON_SECONDS
        dp = v.get("deal_pct")
        scoreable = v.get("basis") not in (None, "insufficient", "no-year")
        is_cand = dp is not None and scoreable and dp >= ENRICH_DEAL_MARGIN
        if is_soon:
            soon.append(r)
        elif is_cand:
            deal_cands.append(r)
        else:
            rest.append(r)
    soon.sort(key=lambda r: r.get("_ends_ts") if r.get("_ends_ts") is not None else float("inf"))
    # deterministic rolling sample: rotate by day-of-year so the rest fills in over runs
    doy = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc).timetuple().tm_yday
    rest.sort(key=lambda r: r.get("id") or 0)
    step = max(1, len(rest) // ENRICH_SAMPLE_TARGET) if rest else 1
    sample = [r for i, r in enumerate(rest) if (i + doy) % step == 0]
    return (soon + deal_cands + sample)[:ENRICH_CAP]


def run(args) -> int:
    fixtures_dir = args.fixtures_dir or os.path.join(_repo_root(), "fixtures")
    out_path = args.out or os.path.join(_repo_root(), "data", "auctions.json")
    only = [c.strip() for c in args.only.split(",")] if args.only else None

    # 1. fetch board
    try:
        if args.offline:
            html = read_text(os.path.join(fixtures_dir, "bat_auctions.html"))
            source_label = "fixtures/bat_auctions.html"
        else:
            html = fetch_auctions_html()
            source_label = "live bringatrailer.com/auctions/"
    except BlockedError as e:
        print(f"Stopped cleanly: BaT appears to be blocking automated access.\n  {e}\n"
              f"No data written (existing snapshot, if any, left untouched).", file=sys.stderr)
        return 2
    except (FetchError, OSError) as e:
        print(f"Stopped: could not fetch the auction board.\n  {e}\nNo data written.", file=sys.stderr)
        return 2

    # 2. parse board
    try:
        raw_items = parse.parse_auctions_html(html)
    except ValueError as e:
        print(f"Stopped: could not parse the auction board ({e}). No data written.", file=sys.stderr)
        return 2
    reported = len(raw_items)

    parsed, parse_failures = [], 0
    for raw in raw_items:
        try:
            parsed.append(parse.parse_item(raw))
        except Exception:
            parse_failures += 1
    parsed_ok = len(parsed)

    # 3. live vs ended
    live = [r for r in parsed if r["bid"]["status"] == "live"]
    excluded_as_ended = parsed_ok - len(live)

    # 4. categorize (metadata tags + filter presets; pivot 2026-06-21: no longer a gate).
    for r in live:
        r["category_ids"] = categories.match_categories(r, only=only)
    # The whole board is scored. --only still restricts to those categories for focused runs.
    scored = [r for r in live if r["category_ids"]] if only else live

    # 5. comps loaded BEFORE scoring — deal scoring is free from board price + comps, so the
    #    whole board can be scored without any per-listing enrichment.
    now = time.time()
    comps_path = args.comps_out or os.path.join(_repo_root(), "data", "comps.json")
    comp_pool = comps.load_comps(comps_path)
    harvested = 0
    if args.harvest_comps and not args.offline:
        try:
            fresh = comps.harvest_recent_sold(args.comp_pages)
            harvested = len(fresh)
            comp_pool = comps.merge_comps(comp_pool, fresh, now=now)
            comps.save_comps(comp_pool, comps_path, generated_at=parse.unix_to_iso(int(now)))
        except (BlockedError, FetchError, OSError) as e:
            print(f"Note: comp harvest skipped: {e}", file=sys.stderr)

    ends_by_id = {r.get("id"): r.get("timestamp_end") for r in raw_items}
    for r in scored:
        r["_ends_ts"] = ends_by_id.get(r["id"])

    # 6. value pass 1 — score the WHOLE board for free (no enrichment yet).
    for car in scored:
        car["value"] = value.compute_value(car, comp_pool, now=now)

    # 7. bounded enrichment — fetch engagement/mileage/condition only for a capped
    #    high-value set (ending soon OR deal candidate OR a rolling sample), to keep the
    #    BaT footprint low. Then re-score those cars so the mileage/condition tilt lands.
    enrichment_available = False
    enrich_requests = 0
    enrich_method = "off"
    enriched_ids = set()
    targets, target_ids = [], []
    source = _resolve_enrich_source(args)
    if source != "off":
        targets = _select_enrichment_targets(scored, now)
        target_ids = [r["id"] for r in targets if r["id"] is not None]
    if source != "off" and target_ids:
        years = [r["year"] for r in targets if isinstance(r["year"], int)]
        min_year, max_year = (min(years), max(years)) if years else (None, None)
        try:
            if source == "listing":
                eng, det, enrich_requests = _enrich_per_listing(
                    targets, offline=args.offline, fixtures_dir=fixtures_dir)
                enrich_method = "per-listing"
            else:
                eng, det, enrich_requests = _enrich_bulk(
                    target_ids, offline=args.offline, fixtures_dir=fixtures_dir,
                    min_year=min_year, max_year=max_year, max_pages=args.max_enrich_pages)
                enrich_method = "listings-filter"
            enrichment_available = True
            by_id = {r["id"]: r for r in scored if r["id"] is not None}
            for iid, e in eng.items():
                if iid in by_id:
                    by_id[iid]["engagement"] = e
                    enriched_ids.add(iid)
            for iid, dd in det.items():
                if iid in by_id:
                    by_id[iid]["details"] = dd
            # value pass 2 — only enriched cars, so the tilt re-ranks deal_score
            for iid in enriched_ids:
                by_id[iid]["value"] = value.compute_value(by_id[iid], comp_pool, now=now)
        except BlockedError as e:
            print(f"Note: enrichment stopped (blocked): {e}", file=sys.stderr)
        except (FetchError, OSError) as e:
            print(f"Note: enrichment unavailable: {e}", file=sys.stderr)

    for r in scored:
        r.pop("_ends_ts", None)

    # coverage / counts. Enrichment coverage is over the bounded TARGET set (did the small
    # set we chose to fetch succeed?), not the whole board.
    enriched_count = len(enriched_ids)
    det_miles = sum(1 for r in targets if (r.get("details") or {}).get("miles") is not None)
    det_tmu = sum(1 for r in targets if (r.get("details") or {}).get("tmu"))
    det_cond = sum(1 for r in targets if (r.get("details") or {}).get("condition"))
    valued = sum(1 for r in scored if (r.get("value") or {}).get("fair_value") is not None)
    deals = sum(1 for r in scored if (r.get("value") or {}).get("is_deal"))

    # 7. build + validate
    warnings = []
    if parse_failures:
        warnings.append(f"{parse_failures} of {reported} board items failed to parse.")
    snapshot = build_snapshot(
        live,
        reported_live_count=reported,
        parsed_live_count=len(live),
        enriched_count=enriched_count,
        warnings=warnings,
    )
    metrics = {
        "reported_live_count": reported,
        "parsed_ok_count": parsed_ok,
        "parse_failures": parse_failures,
        "matched_count": len(scored),
        "enrich_attempted": len(targets) if enrichment_available else 0,
        "enriched_count": len(enriched_ids),
        "enrichment_available": enrichment_available,
    }
    errors, val_warnings = validate_snapshot(snapshot, metrics)
    snapshot["warnings"].extend(val_warnings)

    summary = dict(source=source_label, reported=reported, parsed_live=len(live),
                   scored=len(scored), targets=len(targets), enriched=enriched_count,
                   ended=excluded_as_ended,
                   warnings=snapshot["warnings"], enrich_method=enrich_method,
                   enrich_requests=enrich_requests, comp_pool=len(comp_pool),
                   harvested=harvested, valued=valued, deals=deals,
                   det_miles=det_miles, det_tmu=det_tmu, det_cond=det_cond)

    # 8. write or fail
    if errors:
        print("VALIDATION FAILED — no data written:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        _print_summary(**summary, wrote=False)
        return 1

    try:
        write_snapshot(snapshot, out_path)
    except OSError as e:
        print(f"Stopped: could not write snapshot to {out_path}: {e}", file=sys.stderr)
        _print_summary(**summary, wrote=False)
        return 2
    _print_summary(**summary, wrote=True, out_path=out_path)
    return 0


def _resolve_enrich_source(args) -> str:
    if args.no_enrich or args.enrich_source == "off":
        return "off"
    if args.enrich_source in ("listing", "bulk"):
        return args.enrich_source
    return "bulk" if args.offline else "listing"  # auto


def _print_summary(*, source, reported, parsed_live, scored, targets, enriched, ended, warnings,
                   enrich_method, enrich_requests, comp_pool, harvested, valued, deals,
                   det_miles, det_tmu, det_cond, wrote, out_path=None):
    print(f"Source: {source}")
    print(f"Reported live: {reported}")
    print(f"Parsed live: {parsed_live}")
    print(f"Scored (whole board): {scored}")
    print(f"Excluded as ended: {ended}")
    if enrich_method != "off":
        print(f"Enrichment: {enrich_method} ({enrich_requests} request(s)) · "
              f"bounded targets: {targets} → enriched {enriched}")
        pct = f" ({round(100*det_miles/targets)}%)" if targets else ""
        print(f"Mileage parsed: {det_miles}/{targets}{pct} · TMU: {det_tmu} · condition flags: {det_cond}")
    print(f"Comp pool: {comp_pool}" + (f" (+{harvested} harvested)" if harvested else ""))
    print(f"Valued (fair price): {valued} · deals flagged: {deals}")
    print(f"Warnings: {len(warnings)}")
    for w in warnings:
        print(f"  - {w}")
    if wrote:
        print(f"Wrote: {out_path}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m scraper", description="BaT Value Map scraper (v0.2)")
    p.add_argument("--offline", action="store_true", help="use fixtures instead of the network")
    p.add_argument("--fixtures-dir", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--no-enrich", action="store_true", help="alias for --enrich-source off")
    p.add_argument("--enrich-source", choices=["auto", "listing", "bulk", "off"], default="auto",
                   help="engagement source. auto = per-listing live / bulk offline (default)")
    p.add_argument("--max-enrich-pages", type=int, default=25, help="page cap for bulk enrichment")
    p.add_argument("--only", default=None, help="comma-separated category ids to match")
    p.add_argument("--harvest-comps", action="store_true",
                   help="harvest recent sold results into data/comps.json before scoring")
    p.add_argument("--comp-pages", type=int, default=60, help="pages of recent sold to harvest")
    p.add_argument("--comps-out", default=None, help="comps DB path (default ./data/comps.json)")
    args = p.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
