# BaT Value Map — scraper (v0.2, Phase 0/1)

Data layer for a personal tool that maps live Bring a Trailer auctions by price vs
engagement. This phase builds only the **data pipeline**: fetch the live board,
parse it into structured records, tag them by category, enrich the target category
with engagement, validate, and write a snapshot. **No frontend, no comps/deal
scoring, no Cars & Bids, no image downloads** (thumbnail URLs only).

## Run

```bash
python -m scraper                # fetch live, write data/auctions.json
python -m scraper --offline      # build from fixtures/ (no network)
python -m scraper --no-enrich    # skip engagement enrichment
python -m scraper --enrich-source bulk   # use listings-filter instead of per-listing
python -m scraper --only air-cooled-911-family
```

Example output (real run, 2026-06-20):

```
Source: live bringatrailer.com/auctions/
Reported live: 1194
Parsed live: 1169
Target-category matches: 33
Enriched with comments: 33
Excluded as ended: 25
Enrichment: per-listing (33 request(s))
Warnings: 0
Wrote: data/auctions.json
```

## Tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```

Tests run entirely against committed fixtures (no network).

## Map (web/)

`web/index.html` is a self-contained map (ECharts, no build step): live auctions plotted
by bid price (X, log) vs engagement heat (Y, comments or watchers), each dot a car photo,
colored by category, click → BaT listing, legend toggles categories, scroll/drag to zoom.

```bash
python -m scraper            # refresh data/auctions.json
python3 -m http.server       # from the repo root
# open http://localhost:8000/web/
```

It must be served over http (a browser blocks `file://` from fetching the JSON). deck.gl
+ a build step is the planned upgrade if/when GPU overlap-declutter is needed; for a
two-person tool this single file does the job.

The map also shows value: a **Deal % vs comps** heat axis, fair-value / comp count /
trend in each car's tooltip, and a gold ring on cars flagged as deals (under comps **and**
ending soon — see Comps below).

## Deploy (GitHub Pages + daily auto-refresh)

The repo is git-initialized with a GitHub Actions workflow that refreshes the data daily
and a static map that GitHub Pages serves. One-time setup (your GitHub account):

1. Create a repo and push:
   ```bash
   git remote add origin https://github.com/<you>/BAT-Scanner.git
   git push -u origin main
   ```
2. **Settings → Pages → Build and deployment → Source: Deploy from a branch → `main` / `/ (root)`.**
3. **Settings → Actions → General → Workflow permissions → Read and write permissions** (lets the daily job commit fresh data).
4. Trigger once now: **Actions → "Update BaT data" → Run workflow** (or wait for 13:00 UTC).
5. Bookmark **`https://<you>.github.io/BAT-Scanner/`** — that's the live map for you and your dad.

The `.github/workflows/update.yml` job runs `python -m scraper --harvest-comps` daily,
commits `data/auctions.json` + `data/comps.json`, and Pages redeploys automatically. No
server, no cost.

## Comps, fair value, and deals (Phase 3)

`data/comps.json` is a pool of recent **sold** results (real sales only — "Bid to"
reserve-not-met lots are excluded), kept for ~3 years and limited to our categories. It
**accumulates**: each daily run harvests recent sales and merges them in (BaT's per-model
history isn't cheaply backfillable), so the pool is thin at first and gets richer — and
appreciation trends become computable — over the first few weeks.

Per live car, `scraper/value.py` finds comps (same make+model within a year band, widening
to category if sparse) and records, in the car's `value` block:

- `fair_value` — median sold price of the comps used; `n_comps` / `basis` show how many and how matched
- `deal_pct` — how far the current bid sits below (or above) fair value
- `is_deal` — flagged only when it's under comps by a margin **and ending within ~48h**
  **and** there are enough comps. (A live bid is low early and ramps at the close, so
  "cheap" only means a likely deal near the end.)
- `appreciation_pct` — recent vs older comp medians, once there's enough history (else `null`)

Thresholds live at the top of `value.py`; everything stays `null`/`false` when comps are
too thin to trust.

## Data source

BaT has no official public API. The pipeline uses:

- `GET /auctions/` — the page embeds the entire live board as a JSON blob
  (`var auctionsCurrentInitialData`). One request snapshots all live auctions.
  Engagement (`comments`/`watchers`/`views`) is **null** on this blob.
- Engagement enrichment (the heat axis) is read **per listing page** for the small
  matched-category set — one request per matched car. Each listing page exposes
  `N watchers` and `N Comments` reliably. `views` is not published on the page, so
  it stays null.
- `POST /wp-json/bringatrailer/1.0/data/listings-filter` also carries engagement
  and is available via `--enrich-source bulk`, but its default ordering does not
  surface live auctions in a pageable way (observed ~0% live coverage on
  2026-06-20), so it is **not** the default. It still powers the offline fixture
  test of the join logic.

These are undocumented internal endpoints; they can change without notice. The
fetcher uses a descriptive User-Agent, a >=1s crawl delay, sequential low-volume
requests, no anti-bot bypass, and stops cleanly (no data written) if it sees a
block/challenge.

> Bring a Trailer's Terms of Use prohibit automated extraction. This tool is for
> low-volume, private, personal use and is not redistributed.

## Snapshot schema

`data/auctions.json`:

```json
{
  "schema_version": 1,
  "scraped_at": "ISO-8601 UTC",
  "source": { "reported_live_count": 0, "parsed_live_count": 0, "enriched_count": 0 },
  "warnings": [],
  "auctions": [ /* one per LIVE auction, each tagged with category_ids */ ]
}
```

Each auction: `id, title, year, make{id,name,slug}, models[{id,name,slug}],
taxonomy_paths, category_ids, bid{amount,currency,status},
engagement{comments,views,watchers}, started_at, ends_at,
flags{no_reserve,premium,alumni}, listing_url, thumbnail_url,
details{miles,odometer_raw,tmu,condition[]}, value{...}`.

`details` (mileage + condition) is parsed from the matched car's listing page during the
same per-listing enrichment fetch (no extra requests). It comes from the "BaT Essentials →
Listing Details" bullet list only, so the related-listings sidebar and comments (which name
other cars' mileage) can't contaminate it. `miles` is best-effort (converted from km when a
listing only gives kilometers); `tmu` flags "true mileage unknown" so the number isn't
trusted; `condition` is a list of flags (`numbers-matching`, `repaint`, `restored`,
`rebuilt-engine`, `engine-swap`, `restomod`, `modified`, `replica`, `tribute`, …). Measured
hit-rate on a live 20-car sample (2026-06-20): **mileage 90%, condition 30%** (condition is
sparser but precise). The run summary prints this coverage each time.

The snapshot stores **all** live auctions (not just the target category) so future
categories need no re-scrape; `category_ids` tags which belong where. Engagement is
fetched only for matched-category records to keep request volume low.

### Categories

Five taste categories ship today (a record can match more than one). Defined in
`scraper/categories.py` — `air-cooled-911-family` is a bespoke predicate; the rest are
declarative specs (makes + model/body tokens + year range + exclusions), with
collision-prone tokens scoped per-make (so a Honda "K20" engine swap isn't read as a
Chevy "K20" truck). Live counts on a representative board: ~209 of ~1,170 live cars.

| id | what | year |
|----|------|------|
| `air-cooled-911-family` | Porsche 911/912/930/964/993 | 1964–1998 |
| `60s-muscle` | American muscle (GTO, Chevelle SS, Charger R/T, Mustang, 442, GSX…) | 1964–1972 |
| `80s-90s-japanese` | JDM/enthusiast (Supra, RX-7, MR2, NSX, Skyline GT-R, Miata, Integra…) | 1980–1999 |
| `vintage-trucks` | Pickups & truck-SUVs (F-100, C/K, Bronco, Land Cruiser, Power Wagon, CJ, Scout…) | 1940–1995 |
| `german-wagons` | German estates (Avant, Touring, Variant, Mercedes T-codes, Cross Turismo…) | 1965–2026 |

All categories exclude obvious non-cars (parts/engine-only lots, rollers, shells,
replicas/tributes, scale models). Restrict a run to one with `--only <id>`.

## Known schema uncertainties

- **make/model are derived from the listing title.** The board blob has no
  structured make/model object, so `make.id` and `models[].id` are always `null`
  and the names are best-effort. Category matching gates on `make == Porsche` plus
  a generation-token regex over the title, so it does not depend on perfect
  make/model parsing.
- **`started_at` is always `null`** — BaT does not expose an auction start
  timestamp on this endpoint.
- **`flags.alumni` is always `null`** — not provided by the source.
- **Engagement source: per-listing, not bulk.** The original plan assumed the bulk
  listings-filter endpoint could supply engagement. Live testing showed its ordering
  is volatile and yields ~0% coverage for the live matched set, so the pipeline reads
  each matched listing page instead (100% coverage observed: 33/33 on 2026-06-20).
  `engagement.views` is always null because BaT does not publish a view count on the
  listing page. Enrichment stays best-effort: a per-listing fetch that fails is
  skipped, and coverage below 95% becomes a warning (not a failure). The bulk path is
  retained behind `--enrich-source bulk` and powers the offline fixture test.
- **Validation thresholds:** parsed-coverage ≥ 98% (else fail, no write),
  enrichment-coverage ≥ 95% (else warning), zero duplicate ids, zero invalid
  listing URLs, non-empty dataset, and bid-shape checks (wrong amount type fails;
  unknown currency or null live amount warns).
