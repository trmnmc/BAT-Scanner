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
    eng, requests = {}, 0
    for rec in matched:
        try:
            html = fixture_html if offline else fetch_listing_html(rec["listing_url"])
            requests += 1
            eng[rec["id"]] = parse.parse_listing_engagement(html)
        except (BlockedError, FetchError, OSError) as e:
            print(f"Note: could not enrich listing {rec.get('id')}: {e}", file=sys.stderr)
    return eng, requests


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
        return {i: page_map[i] for i in matched_ids if i in page_map}, 1

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
    return eng, pages_used


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

    # 4. categorize live records
    for r in live:
        r["category_ids"] = categories.match_categories(r, only=only)
    matched = [r for r in live if r["category_ids"]]
    matched_ids = [r["id"] for r in matched if r["id"] is not None]

    # 5. enrich the matched set
    enrichment_available = False
    enrich_requests = 0
    enrich_method = "off"
    enriched_ids = set()
    source = _resolve_enrich_source(args)
    if source != "off" and matched_ids:
        years = [r["year"] for r in matched if isinstance(r["year"], int)]
        min_year, max_year = (min(years), max(years)) if years else (None, None)
        try:
            if source == "listing":
                eng, enrich_requests = _enrich_per_listing(
                    matched, offline=args.offline, fixtures_dir=fixtures_dir)
                enrich_method = "per-listing"
            else:
                eng, enrich_requests = _enrich_bulk(
                    matched_ids, offline=args.offline, fixtures_dir=fixtures_dir,
                    min_year=min_year, max_year=max_year, max_pages=args.max_enrich_pages)
                enrich_method = "listings-filter"
            enrichment_available = True
            by_id = {r["id"]: r for r in matched if r["id"] is not None}
            for iid, e in eng.items():
                if iid in by_id:
                    by_id[iid]["engagement"] = e
                    enriched_ids.add(iid)
        except BlockedError as e:
            print(f"Note: enrichment stopped (blocked): {e}", file=sys.stderr)
        except (FetchError, OSError) as e:
            print(f"Note: enrichment unavailable: {e}", file=sys.stderr)

    # "Enriched with comments" is the user-facing count; coverage validation uses
    # the set of records we actually fetched engagement for (so a fetched listing
    # whose comment count happens to be 0/absent still counts as enriched).
    enriched_with_comments = sum(1 for r in matched if r["engagement"]["comments"] is not None)
    enriched_count = enriched_with_comments

    # 6. comps + value: load the comp pool, optionally grow it, score each matched car
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
    valued = deals = 0
    for car in matched:
        car["_ends_ts"] = ends_by_id.get(car["id"])
        car["value"] = value.compute_value(car, comp_pool, now=now)
        car.pop("_ends_ts", None)
        if car["value"]["fair_value"] is not None:
            valued += 1
        if car["value"]["is_deal"]:
            deals += 1

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
        "matched_count": len(matched),
        "enrich_attempted": len(matched_ids) if enrichment_available else 0,
        "enriched_count": len(enriched_ids),
        "enrichment_available": enrichment_available,
    }
    errors, val_warnings = validate_snapshot(snapshot, metrics)
    snapshot["warnings"].extend(val_warnings)

    summary = dict(source=source_label, reported=reported, parsed_live=len(live),
                   matches=len(matched), enriched=enriched_count, ended=excluded_as_ended,
                   warnings=snapshot["warnings"], enrich_method=enrich_method,
                   enrich_requests=enrich_requests, comp_pool=len(comp_pool),
                   harvested=harvested, valued=valued, deals=deals)

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


def _print_summary(*, source, reported, parsed_live, matches, enriched, ended, warnings,
                   enrich_method, enrich_requests, comp_pool, harvested, valued, deals,
                   wrote, out_path=None):
    print(f"Source: {source}")
    print(f"Reported live: {reported}")
    print(f"Parsed live: {parsed_live}")
    print(f"Target-category matches: {matches}")
    print(f"Enriched with comments: {enriched}")
    print(f"Excluded as ended: {ended}")
    if enrich_method != "off":
        print(f"Enrichment: {enrich_method} ({enrich_requests} request(s))")
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
