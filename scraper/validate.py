"""Validation gate.

Splits problems into errors (block the write; never produce bad data) and warnings
(written into the snapshot, surfaced in the run summary, but allowed).

Thresholds (from the v0.2 plan):
  - parsed live coverage            >= 98%   -> else ERROR
  - engagement enrichment coverage  >= 95%   -> else WARNING (only when enrichment ran)
  - duplicate ids                   == 0     -> else ERROR
  - invalid listing urls            == 0     -> else ERROR
  - empty dataset                            -> ERROR
  - bid amount wrong type / missing bid      -> ERROR  (severe shape problem)
  - unknown currency / null live bid amount  -> WARNING (soft shape problem)
"""

from __future__ import annotations

from urllib.parse import urlparse

PARSE_COVERAGE_MIN = 0.98
ENRICH_COVERAGE_MIN = 0.95
KNOWN_CURRENCIES = {"USD", "CAD", "EUR", "GBP", "AUD", "CHF", "JPY", "SEK", "NOK", "DKK"}


def _valid_listing_url(url) -> bool:
    if not isinstance(url, str) or not url:
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.netloc or "").lower()
    return host == "bringatrailer.com" or host.endswith(".bringatrailer.com")


def validate_snapshot(snapshot: dict, metrics: dict | None = None) -> tuple[list, list]:
    """Return (errors, warnings). metrics carries pre-filter counts the snapshot
    cannot show on its own (parse failures, enrichment attempts)."""
    errors: list[str] = []
    warnings: list[str] = []
    metrics = metrics or {}

    auctions = snapshot.get("auctions")
    if not isinstance(auctions, list) or len(auctions) == 0:
        errors.append("Empty dataset: no auctions in snapshot.")
        # Without rows the rest of the checks are meaningless.
        return errors, warnings

    # missing ids (can't dedupe or enrich a record with no id) -> warning, not error
    missing_id = sum(1 for a in auctions if a.get("id") is None)
    if missing_id:
        warnings.append(f"Records with no id: {missing_id} (cannot be enriched or deduped).")

    # duplicate ids (ignore the None bucket, which is handled above)
    ids = [a.get("id") for a in auctions if a.get("id") is not None]
    seen, dupes = set(), set()
    for i in ids:
        if i in seen:
            dupes.add(i)
        seen.add(i)
    if dupes:
        errors.append(f"Duplicate ids: {len(dupes)} ({sorted(dupes)[:5]}{'...' if len(dupes) > 5 else ''}).")

    # invalid listing urls
    bad_urls = [a.get("id") for a in auctions if not _valid_listing_url(a.get("listing_url"))]
    if bad_urls:
        errors.append(f"Invalid listing urls: {len(bad_urls)} (ids {bad_urls[:5]}).")

    # bid shape
    bad_bid_type = []
    null_live_amount = []
    unknown_currency = set()
    for a in auctions:
        bid = a.get("bid")
        if not isinstance(bid, dict):
            bad_bid_type.append(a.get("id"))
            continue
        amount = bid.get("amount")
        if amount is not None and not isinstance(amount, int):
            bad_bid_type.append(a.get("id"))
        if amount is None and bid.get("status") == "live":
            null_live_amount.append(a.get("id"))
        cur = bid.get("currency")
        if amount is not None and (cur is None or cur not in KNOWN_CURRENCIES):
            unknown_currency.add(cur)
    if bad_bid_type:
        errors.append(f"Bad bid shape (amount not int/None or bid missing): {len(bad_bid_type)} "
                      f"(ids {bad_bid_type[:5]}).")
    if null_live_amount:
        warnings.append(f"Live auctions with null bid amount: {len(null_live_amount)}.")
    if unknown_currency:
        warnings.append(f"Unknown/missing currency codes: {sorted(str(c) for c in unknown_currency)}.")

    # enrichment timestamps: optional, must be null or an ISO-ish string. Malformed stamps are
    # a soft problem (a temporary enrichment hiccup must never block the write).
    bad_stamp = 0
    for a in auctions:
        enr = a.get("enrichment")
        if enr is None:
            continue
        if not isinstance(enr, dict):
            bad_stamp += 1
            continue
        for k in ("engagement_updated_at", "details_updated_at"):
            v = enr.get(k)
            if v is not None and not isinstance(v, str):
                bad_stamp += 1
    if bad_stamp:
        warnings.append(f"Malformed enrichment timestamps on {bad_stamp} record(s).")

    # optional normalized blocks (Stage 1): vehicle_identity + analysis are additive and may be
    # absent. Absence is ALWAYS valid; only a malformed block earns a soft warning — these blocks
    # must never block the write. We check the block shape plus the one field whose type matters
    # most: an identity `year` must be int/None, and an analysis `score` must be number/None
    # (a missing score is null, never 0 — bool is rejected so True/False can't pose as a score).
    bad_identity = 0
    bad_analysis = 0
    for a in auctions:
        vi = a.get("vehicle_identity")
        if vi is not None:
            if not isinstance(vi, dict):
                bad_identity += 1
            else:
                year = vi.get("year")
                if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
                    bad_identity += 1
        an = a.get("analysis")
        if an is not None:
            if not isinstance(an, dict):
                bad_analysis += 1
            else:
                score = an.get("score")
                if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))):
                    bad_analysis += 1
    if bad_identity:
        warnings.append(f"Malformed optional vehicle_identity on {bad_identity} record(s).")
    if bad_analysis:
        warnings.append(f"Malformed optional analysis on {bad_analysis} record(s).")

    # parse coverage (needs pre-filter counts)
    reported = metrics.get("reported_live_count")
    parsed_ok = metrics.get("parsed_ok_count")
    if reported and parsed_ok is not None:
        coverage = parsed_ok / reported if reported else 0.0
        if coverage < PARSE_COVERAGE_MIN:
            errors.append(
                f"Parsed coverage {coverage:.1%} below {PARSE_COVERAGE_MIN:.0%} "
                f"({parsed_ok}/{reported} items parsed)."
            )

    # enrichment coverage (only when enrichment actually ran)
    if metrics.get("enrichment_available"):
        attempted = metrics.get("enrich_attempted", 0)
        enriched = metrics.get("enriched_count", 0)
        if attempted:
            cov = enriched / attempted
            if cov < ENRICH_COVERAGE_MIN:
                warnings.append(
                    f"Engagement enrichment coverage {cov:.1%} below {ENRICH_COVERAGE_MIN:.0%} "
                    f"({enriched}/{attempted} category records enriched)."
                )

    return errors, warnings
