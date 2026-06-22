"""Parse BaT responses into normalized auction records.

Two inputs:
  - the /auctions/ HTML, which embeds `var auctionsCurrentInitialData = {..}`
    (the full live board; engagement fields are null here)
  - the listings-filter JSON, used only for engagement enrichment

Normalized record shape (one per live auction):

    {
      "id", "title", "year",
      "make": {"id", "name", "slug"},
      "models": [{"id", "name", "slug"}],
      "taxonomy_paths": [...],
      "category_ids": [...],            # filled by categories.match_categories
      "bid": {"amount", "currency", "status"},
      "engagement": {"comments", "views", "watchers"},
      "started_at",                     # always null: BaT exposes no start ts here
      "ends_at",
      "flags": {"no_reserve", "premium", "alumni"},  # alumni null: not exposed
      "listing_url", "thumbnail_url"
    }

Schema uncertainties (see README): make/model are derived from the title because
the live board carries no structured make/model object; `started_at` and
`flags.alumni` are null because the source does not provide them.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import time

_INITIAL_DATA_MARKER = "auctionsCurrentInitialData"
# Anchor on the *assignment* (name = {), not any textual mention of the name, so a
# reference inside a comment/string/analytics tag can't hijack the extraction.
_ASSIGN_RE = re.compile(r"auctionsCurrentInitialData\s*=\s*\{")

# Makes whose names are more than one word; checked before the single-token fallback.
_MULTIWORD_MAKES = {
    "alfa romeo", "aston martin", "austin healey", "austin-healey",
    "land rover", "range rover", "rolls-royce", "rolls royce",
    "mercedes-benz", "mercedes benz", "de tomaso", "general motors",
    "international harvester",
}

_YEAR_RE = re.compile(r"\b(18|19|20)\d{2}\b")
_LEADING_DESCRIPTOR_RE = re.compile(r"^[\s\w./%+,&-]*?(?=\b(?:18|19|20)\d{2}\b)")


def _scan_balanced_object(s: str, start: int) -> str | None:
    """Return the substring of s for the brace-balanced object starting at s[start]=='{'.

    String-aware (escapes and braces inside strings are ignored). None if unbalanced.
    """
    depth = 0
    in_str = False
    esc = False
    for k in range(start, len(s)):
        c = s[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[start:k + 1]
    return None


def extract_initial_data(html: str) -> dict:
    """Pull the `auctionsCurrentInitialData = {..}` JSON object out of the HTML.

    Anchors on the assignment and tries each match until one parses to a dict with
    an `items` list (so a stray mention of the name elsewhere can't derail it).
    Uses a brace-balanced, string-aware scan. Raises ValueError if none is found.
    """
    last_err = None
    for m in _ASSIGN_RE.finditer(html):
        brace = html.find("{", m.start())
        if brace < 0:
            continue
        blob = _scan_balanced_object(html, brace)
        if blob is None:
            continue
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError as e:
            last_err = e
            continue
        if isinstance(obj, dict) and isinstance(obj.get("items"), list):
            return obj
    raise ValueError(
        f"auctionsCurrentInitialData object with items[] not found in HTML ({last_err})"
    )


def parse_auctions_html(html: str) -> list[dict]:
    """Return the raw item dicts from the embedded live board."""
    data = extract_initial_data(html)
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("auctionsCurrentInitialData has no 'items' list")
    return items


# ---------------------------------------------------------------------------
# field helpers
# ---------------------------------------------------------------------------

def parse_year(raw_item: dict) -> int | None:
    """Year is a string like '1985' in the source; fall back to the title."""
    y = raw_item.get("year")
    if isinstance(y, int):
        return y
    if isinstance(y, str) and y.strip().isdigit():
        return int(y.strip())
    m = _YEAR_RE.search(raw_item.get("title", "") or "")
    return int(m.group(0)) if m else None


def parse_money(value) -> int | None:
    """current_bid is usually an int, occasionally a string like '136000'."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        return int(digits) if digits else None
    return None


def parse_engagement_value(value) -> int | None:
    """Engagement comes as None (live board) or strings: '0', '8 Watchers', '230 Views'."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = re.search(r"[\d,]+", value)
        if not m:
            return None
        return int(m.group(0).replace(",", ""))
    return None


def unix_to_iso(ts) -> str | None:
    if ts in (None, "", 0):
        return None
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def bid_status(raw_item: dict, now: float | None = None) -> str:
    """live | sold | reserve_not_met | ended.

    Live board items have empty sold_text and a future end time. We still check,
    because an auction can flip to ended between the snapshot and our read.
    """
    now = time.time() if now is None else now
    sold_text = (raw_item.get("sold_text") or "").strip().lower()
    if sold_text.startswith("sold for"):
        return "sold"
    if sold_text.startswith("bid to"):
        return "reserve_not_met"
    if sold_text:
        return "ended"
    ts = raw_item.get("timestamp_end")
    try:
        if ts is not None and int(ts) <= now:
            return "ended"
    except (TypeError, ValueError):
        pass
    return "live"


def is_live(raw_item: dict, now: float | None = None) -> bool:
    return bid_status(raw_item, now) == "live"


def derive_make_models(title: str, url: str = "") -> tuple[dict, list, list]:
    """Best-effort make/model from the title (the board has no structured field).

    Returns (make, models, taxonomy_paths). make.id and model.id are always None;
    the source does not expose stable make/model ids on the board.
    """
    title = title or ""
    after_year = ""
    m = _YEAR_RE.search(title)
    if m:
        after_year = title[m.end():].strip()
    else:
        after_year = title.strip()

    tokens = after_year.split()
    make_name = None
    rest = tokens
    if tokens:
        two = " ".join(tokens[:2]).lower().rstrip(",")
        if two in _MULTIWORD_MAKES:
            make_name = " ".join(tokens[:2])
            rest = tokens[2:]
        else:
            make_name = tokens[0]
            rest = tokens[1:]

    make = {
        "id": None,
        "name": make_name,  # original casing from the title, e.g. "Porsche"
        "slug": slugify(make_name) if make_name else None,
    }

    model_name = rest[0] if rest else None
    models = []
    taxonomy_paths = []
    if model_name:
        model_name = model_name.rstrip(",")
        models.append({"id": None, "name": model_name, "slug": slugify(model_name)})
        if make.get("slug"):
            taxonomy_paths.append(f"{make['slug']}/{slugify(model_name)}")
    return make, models, taxonomy_paths


def parse_item(raw_item: dict, now: float | None = None) -> dict:
    """Normalize one raw board item. Engagement comes from the board (usually null);
    listings-filter enrichment overlays it later. category_ids start empty."""
    title = raw_item.get("title") or ""
    make, models, taxonomy_paths = derive_make_models(title, raw_item.get("url", ""))
    return {
        "id": raw_item.get("id"),
        "title": title,
        "year": parse_year(raw_item),
        "make": make,
        "models": models,
        "taxonomy_paths": taxonomy_paths,
        "category_ids": [],
        "bid": {
            "amount": parse_money(raw_item.get("current_bid")),
            "currency": raw_item.get("currency"),
            "status": bid_status(raw_item, now),
        },
        "engagement": {
            "comments": parse_engagement_value(raw_item.get("comments")),
            "views": parse_engagement_value(raw_item.get("views")),
            "watchers": parse_engagement_value(raw_item.get("watchers")),
        },
        "started_at": None,  # not exposed by the source
        "ends_at": unix_to_iso(raw_item.get("timestamp_end")),
        "flags": {
            "no_reserve": bool(raw_item.get("noreserve")),
            "premium": bool(raw_item.get("premium")),
            "alumni": None,  # not exposed by the source
        },
        "listing_url": raw_item.get("url"),
        "thumbnail_url": raw_item.get("thumbnail_url"),
        "details": None,  # filled for matched cars from the listing page (mileage/condition)
        "value": None,  # filled for matched cars when comps are available (see value.py)
    }


_WATCHERS_RE = re.compile(r"(\d[\d,]*)\s*watchers\b", re.IGNORECASE)
_BIDS_RE = re.compile(r">\s*Bids\s*</td>\s*<td[^>]*>\s*([\d,]+)", re.IGNORECASE)
_COMMENTS_REALTIME_RE = re.compile(r'comments-updated\.display"[^>]*>\s*([\d,]+)\s*comments', re.IGNORECASE)
_COMMENTS_RE = re.compile(r"([\d,]+)\s+comments\b", re.IGNORECASE)


def parse_listing_engagement(html: str) -> dict:
    """Extract engagement from a single listing page's HTML.

    Returns {comments, views, watchers}. `views` is None: BaT does not publish a
    view count on the listing page. `bids` is available on the page but is not part
    of the engagement schema, so it is not returned here.
    """
    def _num(m):
        return int(m.group(1).replace(",", "")) if m else None

    comments = _num(_COMMENTS_REALTIME_RE.search(html))
    if comments is None:
        comments = _num(_COMMENTS_RE.search(html))
    return {
        "comments": comments,
        "views": None,  # not published on the listing page
        "watchers": _num(_WATCHERS_RE.search(html)),
    }


# --- listing details: mileage + condition (Phase 3) -----------------------
# The subject car's facts live in the "BaT Essentials > Listing Details" item as a
# clean <ul> of <li> bullets (chassis, mileage, engine, paint, ...). We parse ONLY
# that block, so the related-listings sidebar and comments (which mention other cars'
# mileage) can't contaminate the read.
_LISTING_DETAILS_RE = re.compile(
    r"<strong>\s*Listing Details\s*</strong>\s*<ul>(.*?)</ul>", re.IGNORECASE | re.DOTALL)
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_MILES_RE = re.compile(r"(\d[\d,]*)\s*([kK])?\s*miles", re.IGNORECASE)
_KM_RE = re.compile(r"(\d[\d,]*)\s*([kK])?\s*(?:kilometers|kilometres|kms?)\b", re.IGNORECASE)

# (flag, pattern) — phrase/word-boundary anchored so "Shell Grey" or "Grand Prix White"
# don't trip a flag (the over-exclusion lesson from category matching).
_CONDITION_PATTERNS = [
    ("numbers-matching", re.compile(r"numbers[\s-]matching|matching[\s-]numbers", re.I)),
    ("original-paint",   re.compile(r"original paint", re.I)),
    ("restored",         re.compile(r"\brestored\b|\brestoration\b", re.I)),
    ("repaint",          re.compile(r"\brepaint(?:ed)?\b|\brespray(?:ed)?\b|\brefinished\b", re.I)),
    ("rebuilt-engine",   re.compile(r"rebuilt\s+(?:\w+\s+){0,2}engine|engine\s+rebuild", re.I)),
    ("restomod",         re.compile(r"resto[\s-]?mod", re.I)),
    ("engine-swap",      re.compile(r"engine\s+swap|\bswapped\b|\bls[\s-]?swap\b|\bv8\s+swap\b|\b[\w.]+-powered\b", re.I)),
    ("modified",         re.compile(r"\bmodified\b|\bmodifications\b", re.I)),
    ("replica",          re.compile(r"\breplica\b|\brecreation\b|re-creation", re.I)),
    ("tribute",          re.compile(r"\btribute\b|\bclone\b", re.I)),
    ("kit-car",          re.compile(r"\bkit\s+car\b", re.I)),
    ("project",          re.compile(r"\bproject\b|non[\s-]running|not\s+running", re.I)),
    ("salvage-title",    re.compile(r"salvage\s+title|rebuilt\s+title|branded\s+title", re.I)),
]


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", s)).strip()


def _num_k(m) -> int:
    """A captured (digits, optional 'k') -> int. '80','k' -> 80000; '45,300','' -> 45300."""
    n = int(m.group(1).replace(",", ""))
    return n * 1000 if m.group(2) else n


def _parse_odometer(bullets):
    """Return (miles, raw_text, tmu) from the listing-detail bullets.

    Prefers a Miles figure; converts from kilometers if only those are given. `tmu`
    flags "true mileage unknown" so downstream code can refuse to trust the number.
    """
    for raw in bullets:
        b = _clean_text(raw)
        low = b.lower()
        if "mile" not in low and "kilometer" not in low and "kilometre" not in low and " km" not in low:
            continue
        tmu = ("tmu" in low) or ("mileage unknown" in low) or ("unknown mileage" in low)
        mm = _MILES_RE.search(b)
        if mm:
            return _num_k(mm), b, tmu
        km = _KM_RE.search(b)
        if km:
            return round(_num_k(km) * 0.621371), b, tmu   # convert km -> mi
        if tmu or "unknown" in low:
            return None, b, True
    return None, None, False


def parse_listing_details(html: str, title: str = "") -> dict:
    """Extract subject-car mileage + condition flags from a listing page.

    Returns {miles, odometer_raw, tmu, condition[]}. All best-effort: a page without a
    parseable "Listing Details" block yields miles=None / condition=[] (not an error).
    """
    m = _LISTING_DETAILS_RE.search(html)
    bullets = _LI_RE.findall(m.group(1)) if m else []
    miles, odo_raw, tmu = _parse_odometer(bullets)
    scan = (title or "") + " " + " ".join(_clean_text(b) for b in bullets)
    condition = [flag for flag, rx in _CONDITION_PATTERNS if rx.search(scan)]
    return {"miles": miles, "odometer_raw": odo_raw, "tmu": tmu, "condition": condition}


def parse_listings_filter(text_or_obj) -> dict:
    """Parse a listings-filter response into {id: engagement_dict}.

    Accepts the raw JSON string or an already-parsed dict.
    """
    obj = json.loads(text_or_obj) if isinstance(text_or_obj, str) else text_or_obj
    out = {}
    for it in obj.get("items", []) or []:
        iid = it.get("id")
        if iid is None:
            continue
        out[iid] = {
            "comments": parse_engagement_value(it.get("comments")),
            "views": parse_engagement_value(it.get("views")),
            "watchers": parse_engagement_value(it.get("watchers")),
        }
    return out
