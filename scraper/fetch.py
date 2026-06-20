"""HTTP access to Bring a Trailer.

Dependency-free (stdlib urllib only) so `python -m scraper` runs with no pip install.

Scraping rules honored here:
  - descriptive User-Agent (identifies the tool + a contact)
  - a Referer on the AJAX endpoint, matching how the real /auctions/ page calls it
  - a polite crawl delay between requests (BaT robots.txt asks for Crawl-delay: 1)
  - low volume, sequential (no concurrency)
  - stop cleanly if blocked: a non-200, a Cloudflare interstitial, or an empty body
    raises BlockedError rather than returning junk.

NOTE: Bring a Trailer's Terms of Use prohibit automated extraction. This module is
for low-volume, private, personal use. It does not attempt any anti-bot bypass.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request

BASE = "https://bringatrailer.com"
AUCTIONS_URL = f"{BASE}/auctions/"
LISTINGS_FILTER_URL = f"{BASE}/wp-json/bringatrailer/1.0/data/listings-filter"

USER_AGENT = (
    "BaT-Value-Map/0.2 (personal research tool; +https://github.com/; contact tfenley23@gmail.com)"
)
DEFAULT_TIMEOUT = 30
CRAWL_DELAY_SECONDS = 1.5  # >= robots.txt Crawl-delay: 1, with margin

# Markers that mean "we got a wall, not data."
_BLOCK_MARKERS = (
    "just a moment",
    "cf-mitigated",
    "attention required",
    "cloudflare",
    "enable javascript and cookies",
)


class FetchError(Exception):
    """Network/transport failure talking to BaT."""


class BlockedError(FetchError):
    """BaT returned a block/challenge instead of data; stop cleanly."""


_last_request_at = 0.0


def _polite_wait() -> None:
    global _last_request_at
    now = time.monotonic()
    wait = CRAWL_DELAY_SECONDS - (now - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _looks_blocked(status: int, body: str) -> bool:
    if status != 200:
        return True
    head = body[:4000].lower()
    return any(m in head for m in _BLOCK_MARKERS)


def _request(url: str, *, data: bytes | None = None, headers: dict | None = None,
             timeout: int = DEFAULT_TIMEOUT) -> str:
    _polite_wait()
    hdrs = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json,*/*"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", resp.getcode())
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        if e.code in (403, 429, 503) or _looks_blocked(e.code, body):
            raise BlockedError(f"HTTP {e.code} from {url} (looks like a block/rate-limit)") from e
        raise FetchError(f"HTTP {e.code} from {url}") from e
    except urllib.error.URLError as e:
        raise FetchError(f"network error for {url}: {e.reason}") from e

    if _looks_blocked(status, body):
        raise BlockedError(f"HTTP {status} from {url} but body looks like a block/challenge")
    if not body.strip():
        raise BlockedError(f"empty body from {url}")
    return body


def fetch_auctions_html(timeout: int = DEFAULT_TIMEOUT) -> str:
    """GET /auctions/ — the page that embeds the full live board as JSON."""
    return _request(AUCTIONS_URL, timeout=timeout)


def fetch_listings_filter_page(page: int, *, minimum_year: int | None = None,
                               maximum_year: int | None = None,
                               timeout: int = DEFAULT_TIMEOUT) -> str:
    """POST one page of the listings-filter endpoint (carries engagement fields).

    Returns the raw JSON text; callers parse it. A Referer matching the real page
    is sent because that is how the site's own AJAX issues the call.
    """
    params = [f"page={int(page)}", "get_items=1"]
    if minimum_year is not None:
        params.append(f"minimum_year={int(minimum_year)}")
    if maximum_year is not None:
        params.append(f"maximum_year={int(maximum_year)}")
    body = "&".join(params).encode("ascii")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Referer": AUCTIONS_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    return _request(LISTINGS_FILTER_URL, data=body, headers=headers, timeout=timeout)


def fetch_listing_html(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """GET a single listing page (used for reliable per-listing engagement).

    The listings-filter endpoint does not surface live auctions in a pageable
    order, so engagement (comments/watchers) for the matched set is read from
    each listing page instead. Low volume: one request per matched car.
    """
    if not (isinstance(url, str) and url.startswith("https://bringatrailer.com/")):
        raise FetchError(f"refusing to fetch non-BaT listing url: {url!r}")
    return _request(url, timeout=timeout)


def read_text(path: str) -> str:
    """Read a local fixture file (used by --offline mode and tests)."""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()
