"""Comparable-sale ("comps") database.

BaT's per-model history pagination is not cheaply scriptable, so instead of one giant
backfill we ACCUMULATE: each run harvests recent *sold* results and merges them into
data/comps.json (deduped, retention-capped). The pool is thin on day one and grows
richer every day — which is also what makes appreciation trends possible over time.

A comp is a completed listing that actually SOLD ("Sold for $X"); reserve-not-met
("Bid to $X") lots are excluded — they are not sales and would bias fair value low.
Every sold car with a year and a price is kept — deal scoring spans the whole board, so
the pool can't be category-locked. `category_ids` is a best-effort tag for the optional
filter presets, not a gate.
"""

from __future__ import annotations

import json
import os
import tempfile

from . import categories, parse
from .fetch import fetch_listings_filter_page

COMP_SCHEMA_VERSION = 1
RETENTION_DAYS = 1095            # keep ~3 years of sales; older comps age out
RETENTION_SECONDS = RETENTION_DAYS * 86400


def parse_completed_item(raw_item: dict) -> dict | None:
    """Turn a raw completed listing into a comp record, or None if it is not a usable
    sale (reserve-not-met, no year, or no price).

    Any sold car with a year + price is kept — deal scoring needs comps across the whole
    board, not just the taste categories. `category_ids` is tagged for the filter presets
    but is no longer a gate (pivot 2026-06-21)."""
    rec = parse.parse_item(raw_item)
    if rec["bid"]["status"] != "sold":          # "Sold for ..." only (not "Bid to")
        return None
    year = rec["year"]
    price = rec["bid"]["amount"]
    if not isinstance(year, int) or not price or price <= 0:
        return None
    cats = categories.match_categories(rec)     # best-effort tag (presets), not a gate
    sold_ts = raw_item.get("sold_text_timestamp") or raw_item.get("timestamp_end")
    try:
        sold_ts = int(sold_ts) if sold_ts is not None else None
    except (TypeError, ValueError):
        sold_ts = None
    return {
        "id": rec["id"],
        "title": rec["title"],
        "make": (rec["make"] or {}).get("slug"),
        "model": (rec["models"][0]["slug"] if rec["models"] else None),
        "year": year,
        "price": price,
        "sold_ts": sold_ts,
        "category_ids": cats,
    }


def harvest_recent_sold(pages: int, *, fetch_page=None) -> list:
    """Page the listings-filter endpoint and collect recent comps. The endpoint's
    default ordering surfaces recent completed sales; we just take what is there and
    rely on accumulation over time for depth. Stops early on three empty pages."""
    fetch_page = fetch_page or (lambda p: json.loads(fetch_listings_filter_page(p)))
    out, seen, empty = [], set(), 0
    for page in range(1, pages + 1):
        try:
            data = fetch_page(page)
        except Exception:
            break
        items = data.get("items", []) if isinstance(data, dict) else []
        added = 0
        for raw in items:
            c = parse_completed_item(raw)
            if c and c["id"] not in seen:
                seen.add(c["id"])
                out.append(c)
                added += 1
        empty = empty + 1 if added == 0 else 0
        if empty >= 3:
            break
    return out


def merge_comps(existing: list, new: list, *, now: float, retention_seconds: int = RETENTION_SECONDS) -> list:
    """Union by id (newest record wins), then drop comps older than the retention window."""
    by_id = {}
    for c in existing or []:
        if c.get("id") is not None:
            by_id[c["id"]] = c
    for c in new or []:
        if c.get("id") is not None:
            by_id[c["id"]] = c
    cutoff = now - retention_seconds
    return [c for c in by_id.values()
            if c.get("sold_ts") is None or c["sold_ts"] >= cutoff]


def load_comps(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return []
    return doc.get("comps", []) if isinstance(doc, dict) else []


def save_comps(comps: list, path: str, *, generated_at: str | None = None) -> str:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    doc = {"schema_version": COMP_SCHEMA_VERSION, "generated_at": generated_at,
           "count": len(comps), "comps": comps}
    fd, tmp = tempfile.mkstemp(prefix=".comps-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path
