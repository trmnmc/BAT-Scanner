"""Pipeline entry point.  Run with:  python -m scraper

  fetch -> parse -> filter-live -> categorize(metadata) -> carry-cache
        -> score whole board -> bounded enrich -> validate -> write

The snapshot stores ALL live auctions and scores the whole board. Categories are optional
metadata, never a gate. Enrichment (engagement/mileage/condition) is carried forward from the
previous snapshot (scraper/enrichment_cache.py) and only a capped, category-agnostic, quota-
based subset is refreshed over the network each run (see _select_enrichment_targets).

Flags:
  --offline            use fixtures/ instead of the network (also what tests exercise)
  --fixtures-dir DIR   fixtures location (default: ./fixtures)
  --out PATH           snapshot path (default: ./data/auctions.json)
  --no-enrich          no network refresh (cached enrichment is still preserved)
  --max-enrich-pages N cap listings-filter pages fetched (default 25)
  --only CID[,CID]     restrict scoring to these category ids (focus runs only; not a board gate)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import time

from . import categories, comps, enrichment_cache, history, identity, opportunity, parse, value
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
    """Reliable engagement: read each target car's listing page.

    The target set is capped (≤300/run), so this is low volume (one request per car). This is
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
# enrich only a capped high-value set for engagement/mileage/condition badges. Selection is
# QUOTA-based and category-agnostic: placeholder category tags never influence what is fetched.
ENRICH_ENDS_SOON_SECONDS = 48 * 3600   # cars ending within 48h (the ticker / is_deal window)
ENRICH_DEAL_MARGIN = 0.15              # free deal_pct >= this => a deal candidate worth badges
ENRICH_CAP = 300                       # hard per-run cap on per-listing fetches (politeness)
ENRICH_STALE_SECONDS = 72 * 3600       # cached engagement older than this is "stale"
TRUSTED_BASES = ("make-model-y3", "make-model-y7")
# quota per bucket; leftover from a short bucket flows to the others until the cap is filled.
ENRICH_QUOTAS = (("urgent", 180), ("unenriched", 90), ("stale", 20), ("sample", 10))


def _ends_ts_of(r):
    ends = r.get("_ends_ts")
    if isinstance(ends, (int, float)):
        return float(ends)
    return _iso_to_unix(r.get("ends_at"))


def _iso_to_unix(s):
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _is_deal_candidate(r):
    v = r.get("value") or {}
    dp = v.get("deal_pct")
    return bool(r.get("flags", {}).get("no_reserve")
               and v.get("basis") in TRUSTED_BASES
               and dp is not None and dp >= ENRICH_DEAL_MARGIN)


def _is_unenriched(r):
    eng = r.get("engagement") or {}
    det = r.get("details") or {}
    has_eng = eng.get("comments") is not None or eng.get("watchers") is not None
    has_det = det.get("miles") is not None or bool(det.get("condition"))
    return not (has_eng or has_det)


def _engagement_age(r, now):
    ts = _iso_to_unix((r.get("enrichment") or {}).get("engagement_updated_at"))
    return (now - ts) if ts is not None else None


def _select_enrichment_targets(scored, now, *, cap=ENRICH_CAP, quotas=ENRICH_QUOTAS):
    """Deterministic, category-agnostic, quota-based target set, hard-capped at `cap`.

    Buckets (priority order):
      urgent     ending within 48h OR a trusted no-reserve deal candidate
                 -> trusted deal candidates first, then soonest-ending
      unenriched no meaningful engagement AND no usable details
                 -> ending farthest out first (so activity accrues before the final day), id tie-break
      stale      cached engagement older than 72h -> oldest first
      sample     deterministic daily rotation by id (broad coverage over time)

    Each record is taken by at most one bucket (deduped by id). Unused quota from a short
    bucket flows to the others until the cap is filled or candidates run out.
    """
    doy = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc).timetuple().tm_yday

    urgent, unenriched, stale = [], [], []
    for r in scored:
        ends = _ends_ts_of(r)
        is_soon = ends is not None and 0 <= ends - now <= ENRICH_ENDS_SOON_SECONDS
        if is_soon or _is_deal_candidate(r):
            urgent.append(r)
        if _is_unenriched(r):
            unenriched.append(r)
        age = _engagement_age(r, now)
        if age is not None and age > ENRICH_STALE_SECONDS:
            stale.append(r)

    _far = float("inf")
    urgent.sort(key=lambda r: (0 if _is_deal_candidate(r) else 1,
                               _ends_ts_of(r) if _ends_ts_of(r) is not None else _far,
                               r.get("id") or 0))
    unenriched.sort(key=lambda r: (-(_ends_ts_of(r) if _ends_ts_of(r) is not None else 0),
                                    r.get("id") or 0))
    stale.sort(key=lambda r: (_engagement_age(r, now) or 0) * -1)  # oldest (largest age) first
    # daily-rotating sample: order the whole board by id, then rotate the start by day-of-year
    # so a different slice leads each day and coverage cycles through the board over time.
    by_id = sorted(scored, key=lambda r: (r.get("id") or 0))
    off = doy % len(by_id) if by_id else 0
    sample = by_id[off:] + by_id[:off]

    streams = {"urgent": urgent, "unenriched": unenriched, "stale": stale, "sample": sample}
    selected, seen = [], set()

    def _take(lst, limit):
        for r in lst:
            if len(selected) >= cap or (limit is not None and limit <= 0):
                break
            rid = r.get("id")
            if rid is None or rid in seen:
                continue
            seen.add(rid)
            selected.append(r)
            if limit is not None:
                limit -= 1

    for name, quota in quotas:                       # pass 1: honor each bucket's reserved quota
        _take(streams.get(name, []), quota)
    for name, _ in quotas:                            # pass 2: redistribute leftover up to the cap
        if len(selected) >= cap:
            break
        _take(streams.get(name, []), None)
    return selected


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
    # ended records (sold / reserve_not_met / ended) are not written to the snapshot, but Stage 9
    # uses them as terminal observations so the history log can record outcomes when a finished
    # car is still on the fetched board this run.
    ended_records = [r for r in parsed if r["bid"]["status"] != "live"]
    excluded_as_ended = parsed_ok - len(live)

    # 3b. canonical vehicle_identity (Stage 6A): recognize multiword models, flag ambiguous ones,
    #     apply manual overrides. Derived from the already-parsed title — NO extra network requests.
    overrides_path = args.identity_overrides or os.path.join(_repo_root(), "data", "identity_overrides.json")
    overrides = identity.load_overrides(overrides_path)
    for r in live:
        r["vehicle_identity"] = identity.derive_identity(r, overrides)
    low_conf = sum(1 for r in live if identity.is_low_confidence(r.get("vehicle_identity")))

    # 4. categorize (metadata tags + filter presets; pivot 2026-06-21: no longer a gate).
    for r in live:
        r["category_ids"] = categories.match_categories(r, only=only)
    # The whole board is scored. --only still restricts to those categories for focused runs.
    scored = [r for r in live if r["category_ids"]] if only else live

    # 4b. carry cached enrichment forward (engagement / mileage / condition) from the previous
    #     snapshot. Runs BEFORE value scoring so cached mileage/condition affect the score, and
    #     runs regardless of --no-enrich (that flag means "no network refresh", not "erase").
    prev_snapshot, cache_warn = enrichment_cache.load_prev_snapshot(out_path)
    cache_warnings = [cache_warn] if cache_warn else []
    cache_stats = enrichment_cache.carry_forward_enrichment(live, prev_snapshot)

    # 5. comps loaded BEFORE scoring — deal scoring is free from board price + comps, so the
    #    whole board can be scored without any per-listing enrichment.
    now = time.time()
    now_iso = parse.unix_to_iso(int(now))
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

    # Stage 6A: annotate the comp pool with canonical identity ONCE (from each comp's title; no
    # network), so canonical comp matching is consistent and fast for the whole board. Legacy comps
    # (no canonical fields on disk) are upgraded in-memory; the saved comps.json is left untouched.
    score_pool = [identity.annotate_comp(c) for c in comp_pool]

    # 6. value pass 1 — score the WHOLE board for free (no enrichment yet).
    for car in scored:
        car["value"] = value.compute_value(car, score_pool, now=now)

    # 7. bounded enrichment — fetch engagement/mileage/condition only for a capped
    #    high-value set (ending soon OR deal candidate OR a rolling sample), to keep the
    #    BaT footprint low. Then re-score those cars so the mileage/condition tilt lands.
    enrichment_available = False
    enrich_requests = 0
    enrich_method = "off"
    enriched_ids = set()
    refreshed_ids = set()
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
            details_changed_ids = set()
            for iid in set(eng) | set(det):
                rec = by_id.get(iid)
                if rec is None:
                    continue
                e, dd = eng.get(iid), det.get(iid)
                got_eng = bool(e) and (e.get("comments") is not None or e.get("watchers") is not None)
                got_det = bool(dd) and (dd.get("miles") is not None or bool(dd.get("condition")))
                if got_eng:
                    rec["engagement"] = e
                    enriched_ids.add(iid)
                if got_det:
                    rec["details"] = dd
                    details_changed_ids.add(iid)
                if got_eng or got_det:
                    refreshed_ids.add(iid)
                    # stamp only the portions that actually parsed this run
                    enrichment_cache.stamp_enrichment(rec, now_iso=now_iso,
                                                      engagement=got_eng, details=got_det)
            # value pass 2 — recompute only where DETAILS changed (mileage/condition tilt)
            for iid in details_changed_ids:
                by_id[iid]["value"] = value.compute_value(by_id[iid], score_pool, now=now)
        except BlockedError as e:
            print(f"Note: enrichment stopped (blocked): {e}", file=sys.stderr)
        except (FetchError, OSError) as e:
            print(f"Note: enrichment unavailable: {e}", file=sys.stderr)

    # 7b. Stage 6B — opportunity scoring + scarce production badges. Runs AFTER enrichment (so
    #     engagement/condition land first) and BEFORE _ends_ts is dropped (the below-market timing
    #     needs it). Market-only inputs (value/identity/engagement/flags + comps); NO network, and
    #     NO personal data (watchlists/notes/budgets are never read here).
    board_stats = opportunity.build_board_stats(scored)
    for car in scored:
        res = opportunity.evaluate_car(car, score_pool, now=now, board_stats=board_stats, now_iso=now_iso)
        car["estimate"] = res["estimate"]
        car["opportunity"] = res["opportunity"]
        car["analysis"] = res["analysis"]
    badge_tally = opportunity.assign_badges(scored)

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

    # whole-board availability (cached + freshly fetched), for the snapshot's source block.
    def _has_eng(r):
        e = r.get("engagement") or {}
        return e.get("comments") is not None or e.get("watchers") is not None

    def _has_det(r):
        d = r.get("details") or {}
        return d.get("miles") is not None or bool(d.get("condition"))

    engagement_available_count = sum(1 for r in live if _has_eng(r))
    details_available_count = sum(1 for r in live if _has_det(r))
    # engagement present whose timestamp isn't this run = carried from a prior run (cached)
    engagement_cached_count = sum(
        1 for r in live if _has_eng(r)
        and (r.get("enrichment") or {}).get("engagement_updated_at") != now_iso)

    # 8. build + validate
    warnings = list(cache_warnings)
    if parse_failures:
        warnings.append(f"{parse_failures} of {reported} board items failed to parse.")
    snapshot = build_snapshot(
        live,
        scraped_at=now_iso,   # one frozen run timestamp: scraped_at == history observed_at == now
        reported_live_count=reported,
        parsed_live_count=len(live),
        enriched_count=enriched_count,
        enrichment_refreshed_count=len(refreshed_ids),
        engagement_available_count=engagement_available_count,
        engagement_cached_count=engagement_cached_count,
        details_available_count=details_available_count,
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
                   det_miles=det_miles, det_tmu=det_tmu, det_cond=det_cond,
                   carried=cache_stats.get("matched", 0), low_conf=low_conf, badges=badge_tally,
                   eng_avail=engagement_available_count, eng_cached=engagement_cached_count)

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

    # 9. record auction-change events (Stage 9). Runs ONLY after a valid snapshot has been written,
    #    so a failed scrape/validation never touches history. Reuses the already-loaded previous
    #    snapshot as the prior valid state; the current observation is live + ended records so
    #    terminal outcomes are detectable. Wrapped so a history hiccup can never fail the run or
    #    corrupt the already-written snapshot.
    if not args.no_history:
        history_path = args.history_out or os.path.join(os.path.dirname(os.path.abspath(out_path)),
                                                         "history.json")
        try:
            hist = history.load_history(history_path)
            curr_records = list(live) + list(ended_records)
            res = history.record_observation(
                hist, (prev_snapshot or {}).get("auctions"), curr_records,
                observed_at=snapshot["scraped_at"],
                source=f"snapshot:{snapshot['scraped_at']}", valid=True, now=now)
            history.save_history(hist, history_path, generated_at=now_iso)
            print(f"History: +{res['added']} event(s), -{res['removed']} compacted "
                  f"({len(hist['events'])} total) -> {history_path}")
        except (OSError, ValueError) as e:
            print(f"Note: history not updated ({e}); snapshot is unaffected.", file=sys.stderr)
    return 0


def _resolve_enrich_source(args) -> str:
    if args.no_enrich or args.enrich_source == "off":
        return "off"
    if args.enrich_source in ("listing", "bulk"):
        return args.enrich_source
    return "bulk" if args.offline else "listing"  # auto


def _print_summary(*, source, reported, parsed_live, scored, targets, enriched, ended, warnings,
                   enrich_method, enrich_requests, comp_pool, harvested, valued, deals,
                   det_miles, det_tmu, det_cond, carried, low_conf, badges, eng_avail, eng_cached,
                   wrote, out_path=None):
    print(f"Source: {source}")
    print(f"Reported live: {reported}")
    print(f"Parsed live: {parsed_live}")
    print(f"Scored (whole board): {scored}")
    print(f"Excluded as ended: {ended}")
    print(f"Enrichment carried forward from cache: {carried} record(s)")
    if enrich_method != "off":
        print(f"Enrichment: {enrich_method} ({enrich_requests} request(s)) · "
              f"bounded targets: {targets} → refreshed {enriched}")
        pct = f" ({round(100*det_miles/targets)}%)" if targets else ""
        print(f"Mileage parsed: {det_miles}/{targets}{pct} · TMU: {det_tmu} · condition flags: {det_cond}")
    print(f"Engagement available (board): {eng_avail} ({eng_cached} from cache)")
    print(f"Comp pool: {comp_pool}" + (f" (+{harvested} harvested)" if harvested else ""))
    print(f"Valued (fair price): {valued} · deals flagged: {deals}")
    print(f"Low-confidence identities (valuation suppressed): {low_conf}")
    if badges:
        glyph = {"opportunity": "💎", "trophy": "🏆", "hot": "🔥", "warning": "⚠️"}
        print("Badges: " + " · ".join(f"{glyph.get(k, k)} {k} {v}" for k, v in sorted(badges.items())))
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
    p.add_argument("--identity-overrides", default=None,
                   help="manual identity overrides JSON (default ./data/identity_overrides.json)")
    p.add_argument("--history-out", default=None,
                   help="auction history log path (default: history.json beside --out)")
    p.add_argument("--no-history", action="store_true",
                   help="skip recording auction-change events for this run")
    args = p.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
