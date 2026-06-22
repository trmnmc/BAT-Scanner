"""Build and atomically write the data/auctions.json snapshot."""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile

from . import SCHEMA_VERSION


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def build_snapshot(auctions: list, *, reported_live_count: int, parsed_live_count: int,
                   enriched_count: int, warnings: list | None = None,
                   scraped_at: str | None = None,
                   enrichment_refreshed_count: int | None = None,
                   engagement_available_count: int | None = None,
                   engagement_cached_count: int | None = None,
                   details_available_count: int | None = None) -> dict:
    # enriched_count keeps its meaning: records that got engagement THIS run. The additive
    # counts below describe whole-board availability (cached + fresh); the frontend can still
    # derive availability from the auction records themselves and never depends on these.
    source = {
        "reported_live_count": reported_live_count,
        "parsed_live_count": parsed_live_count,
        "enriched_count": enriched_count,
    }
    extra = {
        "enrichment_refreshed_count": enrichment_refreshed_count,
        "engagement_available_count": engagement_available_count,
        "engagement_cached_count": engagement_cached_count,
        "details_available_count": details_available_count,
    }
    for k, v in extra.items():
        if v is not None:
            source[k] = v
    return {
        "schema_version": SCHEMA_VERSION,
        "scraped_at": scraped_at or _now_iso(),
        "source": source,
        "warnings": list(warnings or []),
        "auctions": auctions,
    }


def write_snapshot(snapshot: dict, path: str) -> str:
    """Write JSON atomically (temp file + os.replace) so a crash never leaves a
    half-written data file. Returns the path."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".auctions-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path
