# BaT Value Map

A personal tool that maps the **entire live Bring a Trailer board** so you can spot a good
car at a glance. Two parts, both in this repo:

- a dependency-free Python **scraper** that snapshots the whole live board, scores it against
  a comp pool, enriches a capped subset with engagement/mileage/condition (and **carries that
  enrichment forward** between runs), and writes `data/auctions.json`.
- a no-build **ECharts map** (`web/`) that GitHub Pages serves. The default map is **current
  bid vs time remaining** — every active auction, not a curated subset.

Key product facts:

- The **complete live board is stored and shown by default.** The five categories
  (`scraper/categories.py`) are **optional placeholder metadata** — they never decide which
  auctions appear, get enriched, or how the map/search work, and they're gone from the UI's
  primary navigation. `category_ids` still rides along for old saved views.
- **Activity views (comments/watchers) use partial, cached enrichment.** Engagement is
  refreshed under a **300-request/run cap** and carried forward, so the board fills in over
  time. Missing engagement is shown explicitly (hollow dot / "Activity not scanned yet"),
  never faked as zero.
- **Natural-language search will use a server-side interpreter** that returns a *validated
  filter spec* — it does not match cars itself. The frontend always matches locally through
  the one filter engine (`web/filters.js`). **No LLM provider is wired up in this pass and no
  LLM secret lives in the frontend or repo;** the endpoint URL is a public, empty config.
  Until an endpoint is set, search is honest basic keyword matching over titles/makes/models.

No Cars & Bids, no image downloads (thumbnail URLs only), no build step.

## Run

```bash
python -m scraper                # fetch live, write data/auctions.json
python -m scraper --offline      # build from fixtures/ (no network)
python -m scraper --no-enrich    # no network refresh, but cached enrichment is PRESERVED
python -m scraper --enrich-source bulk   # use listings-filter instead of per-listing
python -m scraper --only air-cooled-911-family   # focus a run (metadata only; not a board gate)
```

`--no-enrich` means "don't hit the network this run," **not** "erase enrichment": the cache
from the previous snapshot is still carried forward.

## Tests & validation

One-time setup:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
```

The standard validation suite (run before completing any change):

```bash
python -m pytest                 # Python unit tests (fixtures only, no network)
node --test web/*.test.js        # pure-JS frontend module tests
node tools/verify_snapshot.js    # map invariants on the committed snapshot
git diff --check                 # no whitespace/conflict markers
```

Whenever Python snapshot generation changes, also rebuild from fixtures and re-verify:

```bash
python -m scraper --offline --out /tmp/bat-auctions.json
node tools/verify_snapshot.js /tmp/bat-auctions.json
```

All tests run entirely against committed fixtures (no network). CI runs the same suite
on every code push and pull request (`.github/workflows/test.yml`); data-only refresh
commits under `data/` are intentionally excluded. The separate `update.yml` workflow only
refreshes and commits data and never runs during tests.

## Map (web/)

`web/index.html` is a self-contained map (ECharts, no build step). Files:

- `web/map-data.js` — pure map logic (active/metric/no-bid-lane/marker-density/freshness),
  shared by the browser and Node tests (`web/map-data.test.js`).
- `web/filters.js` — the **one** filter engine (`matchesFilter`/`filterCars`). Map, search,
  saved views, and a future LLM interpreter all produce the same spec and match here.
- `web/search.js` — search-spec sanitizer + the safe interpreter adapter (`web/search.test.js`).

The default map is **current bid (X, log) vs time remaining (Y, soonest at the top)** for
**every active auction**. Other "Map" axes: Comments, Watchers, Comp discount — those only
plot cars that actually have that data (the count readout says how many don't). Visual state
comes from the data, not categories: filled dot = has activity data, **hollow dot = no
activity scanned yet**, faded = cached >72h, **gold ring = trusted ending-soon deal**.
Zero/no-bid cars sit in a labelled **"No bid" lane** on the left (never faked as a $1 bid).
Marker density is automatic (photos only for a small set; the full board uses dots).

One prominent **search** box (titles/makes/models/taxonomy keyword search today; LLM-ready,
see below), a collapsible **Advanced filters & display** section, a **Map/List** toggle (the
List is a sortable table of the active board), and a click-to-pin **detail card** with a
live countdown and a "View on BaT" button.

```bash
python -m scraper            # refresh data/auctions.json
python3 -m http.server       # from the repo root
# open http://localhost:8000/web/
```

It must be served over http (a browser blocks `file://` from fetching the JSON). Run the
no-hardcoded-totals sanity report any time with `node tools/verify_snapshot.js`.

### Natural-language search (LLM-ready, not wired up this pass)

The search box is **basic keyword AND-search** by default and labelled honestly. To enable
natural-language search later, point `<meta name="bat-search-endpoint">` (or
`window.BAT_SEARCH_ENDPOINT`) at a **server-side** endpoint. The browser POSTs
`{version, query, catalog}` (the catalog is just the unique makes + currencies, not the
dataset); the endpoint returns `{version, summary, spec}`. That spec is run through
`normalizeSearchSpec` + `validateSearchSpec` before it is applied — and on any failure
(timeout, bad JSON, invalid/empty spec) the UI shows a quiet note and falls back to keyword
search. **The LLM is only an interpreter; matching always happens locally through
`BATFilters.filterCars`.** No provider is implemented here and **no API key is in the
frontend, the JS, GitHub Pages config, or any committed file.**

### AI-assisted Auction Brief (optional hook, off by default)

The Auction Brief is **rule-based and deterministic** by default. The same key-free pattern lets an
optional server-side endpoint *explain* it: point `<meta name="bat-brief-endpoint">` (or
`window.BAT_BRIEF_ENDPOINT`) at a server endpoint and the brief POSTs **only public + deterministic
data** (auction facts, the computed value/estimate/opportunity/badges, selected comps, public
activity) — **never** private notes/budget/max-bid/status/tags/findings. The response must match a
strict, size-capped shape where **every claim cites a supplied evidence id**; it may only *explain*
the deterministic fields and **never overwrites** the current bid, estimated range, Opportunity Score,
confidence, badges, reserve status, or risk flags. Any timeout/failure/invalid response keeps the
deterministic brief. The AI layer is rendered separately and labeled **"AI-assisted brief"**. With no
endpoint configured the site behaves exactly as before. Full contract: [`docs/auction-brief-api.md`](docs/auction-brief-api.md)
(`web/assistant.js`). No provider SDK or API key ships in the browser or the repo.

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

### Canonical vehicle identity (Stage 6A)

The board has no structured make/model, so titles are parsed best-effort — and a naive "first
word after the make" reading turns **El Camino** into model "El", **Land Cruiser** into "Land",
and **Grand Wagoneer** into "Grand". Those fragments then pollute the comp pool (every "Grand"
on the board comping together). `scraper/identity.py` fixes this with a **canonical identity**:

- A **data-driven registry** (per-make multiword model names + per-make patterns like Mercedes
  `<number><body-code>`) recognizes multiword models, so the identity is "el-camino", not "el".
  Model knowledge lives in one place — not scattered `if`s through the pipeline.
- A conservative **collision-prefix net** (`el`, `land`, `grand`, `gran`, `monte`, `santa`)
  catches *unregistered* truncations: a bare "grand" with nothing the registry knows after it is
  flagged **low-confidence** rather than silently comped. (Legit standalone models like Regal,
  Sierra, or Ford GT are NOT in the net, so they're never wrongly suppressed.)
- A **low-confidence identity suppresses a trusted valuation** (`value.basis` becomes
  `low-confidence-identity`; no `fair_value`, no `is_deal`) — an ambiguous car never gets a
  confident price.
- **Comp matching prefers the canonical make+model** (legacy comps are upgraded in-memory from
  their title, so the on-disk pool keeps working), and `value.match_reasons` always explains how
  comps were matched — or why they weren't. Comps are **never** blended into a category median.
- **Manual overrides** for genuine one-offs live in `data/identity_overrides.json`, keyed by a
  stable id (`bat:<id>` or the listing URL). Systematic fixes belong in the registry, not here.

Each live record carries a `vehicle_identity` block (additive/optional): `canonical_make`,
`canonical_model`, `generation`, `chassis_code`, `trim`, `body_style`, `engine`, `transmission`,
`drivetrain`, `market`, `originality`, `confidence`, `ambiguity_reasons`, `source`,
`manually_overridden`. The legacy `make`/`models` fields are unchanged. Identity is derived from
the **already-parsed title — no extra network requests**, and `python tools/identity_report.py`
audits low-confidence identities, model collisions, overrides, and cars with no reliable identity.

### Opportunity scoring, estimate ranges & production badges (Stage 6B)

`scraper/opportunity.py` builds **on top of** the `value` block (it never recomputes it) to add three
cautious, explainable, **market-only** things per live car — personal data (watchlists, notes,
budgets) is never an input:

- **`estimate`** — a final-price **band** `{low, high, currency, confidence, model_version,
  comp_ids, adjustment_reasons, reserve_uncertainty}`, never a single point. It's an interdecile
  (p10–p90) comp band, floored to ±12% of the median (one specific car's trim/condition is
  irreducible), **clamped to never fall below the current bid**, and shifted by the deterministic
  mileage/condition tilt. A trusted band requires a trusted comp basis **and** a confident,
  unambiguous identity; otherwise `low/high` are `null` and the status is *Too early to estimate*.
  Reserve auctions carry a separate `reserve_uncertainty` note and a lower confidence. Watchers and
  comments **never** move the value band.
- **`opportunity`** — an Opportunity Score (0–100 | `null`) from four weighted components in one
  config object (`OPPORTUNITY_WEIGHTS`, validated to total 1.0): investment quality 0.40, enthusiast
  appeal 0.25, below-market chance 0.20, auction interestingness 0.15. Each component returns a
  score/confidence/coverage plus machine + readable reasons and the missing inputs. **Missing data
  lowers confidence, never zeroes a score.** A low *early* bid is **not** an opportunity — below-market
  chance is scaled by time-to-close and withheld on reserve lots. Tracking status is one of
  `too_early_to_estimate` / `trading_below_expected` / `tracking_near_expected` /
  `tracking_above_expected`.
- **`analysis`** — fills the Stage-1 placeholder `{score, confidence, summary, basis, flags,
  updated_at}` with the Opportunity Score and a single **approved** cautious phrase.

**Production badges** are selected **board-wide** and emitted as `badges` codes the frontend renders:
Diamond (`opportunity`), Flame (`hot`), Trophy (`trophy`) are scarce **main** badges — each needs
both an absolute score floor **and** a board-percentile, the total is capped at ~12% of the live
board, and **at most one main badge per auction**. Ambiguous/low-confidence identity gets **no
Diamond or Trophy** (and no trusted range). Warning (`warning`, a status badge) flags concrete risk
(bad condition/title, TMU, replica, unconfirmable identity). Radar stays watchlist-driven and Ghost
stays historical. Language is cautious throughout — never "undervalued/overpriced/buy/bargain".

`tools/evaluate_estimates.py` backtests the ranges against **real** sold prices (each comp as a
leave-one-out, no lookahead) and reports the share inside-range, midpoint error, and accuracy by
confidence and by make/model — so estimate quality is measured, not asserted.

## Data source

BaT has no official public API. The pipeline uses:

- `GET /auctions/` — the page embeds the entire live board as a JSON blob
  (`var auctionsCurrentInitialData`). One request snapshots all live auctions.
  Engagement (`comments`/`watchers`/`views`) is **null** on this blob.
- Engagement enrichment is read **per listing page** for the bounded target set (quota-
  based, ≤300/run; see above) — one request per car. Each listing page exposes `N watchers`
  and `N Comments` reliably, plus the mileage/condition `details`. `views` is not published on
  the page, so it stays null. Everything not refreshed this run keeps its carried-forward cache.
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
  "source": {
    "reported_live_count": 0, "parsed_live_count": 0,
    "enriched_count": 0,                 // got engagement THIS run
    "enrichment_refreshed_count": 0,     // listings successfully re-fetched this run
    "engagement_available_count": 0,     // whole board: have engagement (cached + fresh)
    "engagement_cached_count": 0,        // of those, carried from a prior run
    "details_available_count": 0
  },
  "warnings": [],
  "auctions": [ /* one per LIVE auction */ ]
}
```

`schema_version` stays `1`: the new `source` fields and the per-auction `enrichment` block are
**additive and optional**, so existing consumers keep working. The frontend never reads these
counts — it derives availability from the auction records themselves.

Each auction: `id, title, year, make{id,name,slug}, models[{id,name,slug}],
taxonomy_paths, category_ids, bid{amount,currency,status},
engagement{comments,views,watchers}, started_at, ends_at,
flags{no_reserve,premium,alumni}, listing_url, thumbnail_url,
details{miles,odometer_raw,tmu,condition[]}, value{...},
vehicle_identity{canonical_make,canonical_model,…,confidence,ambiguity_reasons} (Stage 6A; see
"Canonical vehicle identity"), enrichment{engagement_updated_at, details_updated_at}`. `value`
also carries `identity_confidence` and `match_reasons`. Stage 6B adds (scored cars only)
`estimate{low,high,currency,confidence,model_version,comp_ids,adjustment_reasons,reserve_uncertainty}`,
`opportunity{score,confidence,tracking,components,…}`, the filled `analysis{score,confidence,summary,
basis,flags,updated_at}`, and a scarce `badges[]` code array — all additive (schema stays `1`).

`enrichment` (optional, auction-level) timestamps when engagement / details were last
successfully fetched, so the map can fade stale activity and say "Activity updated 2h ago".
Stored separately from `engagement`/`details` so the parser return contracts don't change.
Legacy cached records with no timestamps fall back to the previous snapshot's `scraped_at`.

`details` (mileage + condition) is parsed from the matched car's listing page during the
same per-listing enrichment fetch (no extra requests). It comes from the "BaT Essentials →
Listing Details" bullet list only, so the related-listings sidebar and comments (which name
other cars' mileage) can't contaminate it. `miles` is best-effort (converted from km when a
listing only gives kilometers); `tmu` flags "true mileage unknown" so the number isn't
trusted; `condition` is a list of flags (`numbers-matching`, `repaint`, `restored`,
`rebuilt-engine`, `engine-swap`, `restomod`, `modified`, `replica`, `tribute`, …). Measured
hit-rate on a live 20-car sample (2026-06-20): **mileage 90%, condition 30%** (condition is
sparser but precise). The run summary prints this coverage each time.

The snapshot stores **all** live auctions and scores the whole board (board price + comps is
free, no per-listing fetch). Engagement/mileage/condition enrichment is bounded:

- **Carried forward** between runs (`scraper/enrichment_cache.py`): the previous snapshot's
  engagement/details/timestamps are copied onto the matching current records (matched by **id
  AND listing_url**) before scoring, so data never disappears when a listing isn't re-fetched.
  Volatile board fields (bid, ends_at, flags, value, title) are never carried — only enrichment.
- **Refreshed under a 300/run cap**, category-agnostic, via deterministic **quota** buckets
  (`_select_enrichment_targets`): ~180 ending-soon/urgent-deal, ~90 unenriched, ~20 stale
  (>72h), ~10 rotating sample; unused quota flows to the other buckets. Placeholder categories
  have **no** effect on what gets enriched. A failed refresh keeps the last cached data.

### Normalized model (optional, additive — `web/auction-model.js`)

`web/auction-model.js` exposes a backward-compatible *normalized* view over both live auctions
and historical comps. It is **purely additive**: it layers a few normalized fields on top of the
existing raw record without removing or rewriting any raw field, so `schema_version` stays `1`
and every existing consumer keeps working. Legacy records that carry **none** of the new blocks
normalize fine — what's missing is reported honestly as `null`/`ambiguous`, never invented or
treated as zero. Stage 1 only *defines and normalizes* these blocks; it does not compute scores,
draw badges, change map rendering, or rewrite `data/auctions.json`.

- **Marketplace-qualified auction keys** — `auction_key` is `"<marketplace>:<id>"`, e.g.
  `bat:115717336` (built by `auctionKey(marketplace, id)`). `marketplace` defaults to `bat`. A
  record with no `id` gets a `null` key (it can't be deduped or enriched — the same rule the
  validation gate uses). This lets live auctions and historical sales share one keyspace.
- **`historical_status`** — `"live"` for an active board auction, `"sold"` for a historical comp.
- **`vehicle_identity`** (optional) — `{ year, make{slug,name}, model{slug,name}, trim, vin,
  source, ambiguous }`. Built from an explicit `vehicle_identity` block when present, otherwise
  derived from the legacy `year` / `make` / `models` (or a comp's `make` / `model` slug). `source`
  is `"explicit"`, `"legacy"`, or `null`; `ambiguous` is `true` whenever year + make + model can't
  all be pinned down — a later stage must not hang a *confident* valuation on an ambiguous
  identity. An absent or malformed block is valid (it falls back to the legacy fields).
- **`analysis`** (optional) — `{ score, confidence, summary, basis, flags, updated_at }`, all
  `null` until a later stage computes them. `score` is **`null` when unknown — never `0`** (a real
  `0` is a value; missing is not). An absent or malformed `analysis` normalizes to `null`.

The scraper validation emits a soft **warning** for a malformed optional `vehicle_identity` or
`analysis` block (wrong shape, or a `year`/`score` of the wrong type) but never blocks the write;
**absence is always valid**.

### Categories (optional placeholder metadata)

The five categories are **placeholders** — they do not gate the board, enrichment, the map, or
search, and they're gone from the UI's primary navigation. They remain in `scraper/categories.py`
and as `category_ids` only so old saved views keep working and a future filter can reuse them. A
record can match more than one. `air-cooled-911-family` is a bespoke predicate; the rest are
declarative specs (makes + model/body tokens + year range + exclusions), with collision-prone
tokens scoped per-make (so a Honda "K20" engine swap isn't read as a Chevy "K20" truck).

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
