# BaT Scanner implementation rules

## Product

BaT Scanner is a live-market scanner first: a complete, always-on view of the entire
live Bring a Trailer board. AI assistance is a companion that supports the scanner,
never replaces it.

The map remains the primary discovery interface. It must continue showing the entire
live BaT board by default. The Auction Brief, watchlists, scoring, historical context,
and AI explanations support the map rather than replacing it.

## Architecture

Preserve the current architecture unless a stage explicitly says otherwise:

- dependency-free Python scraper
- static no-build frontend
- ECharts visualization
- pure JavaScript modules testable with Node
- GitHub Pages deployment
- additive, backward-compatible JSON schemas
- no API keys or provider secrets in frontend code
- deterministic fallback whenever an AI endpoint is unavailable

Do not introduce React, Vue, TypeScript, a bundler, npm runtime dependencies, a database,
or a server framework without explicit approval.

## Critical invariants

- Show the entire live board by default.
- Never treat missing data as zero.
- Never hide a car merely because enrichment is missing.
- Reserve-auction bids are not sale prices and must not be labeled bargains.
- Ambiguous vehicle identities do not receive confident valuations.
- Historical dots must never use invented metric values.
- AI text may explain deterministic fields but may not silently overwrite them.
- Do not automate bidding or store BaT credentials.
- Existing search, filters, saved views, map/list toggle, countdowns, and BaT links must continue working.
- Preserve mobile behavior and reduced-motion behavior.
- Do not manually edit generated data files to simulate features.
- Do not run live network scraping during tests.
- A failed scrape must not overwrite the last valid snapshot.
- Keep language cautious. Do not use “undervalued,” “overpriced,” “buy,”
  “don’t buy,” or “guaranteed bargain.”

## Required validation

Before completing a stage, run:

python -m pytest
node --test web/*.test.js
node tools/verify_snapshot.js
git diff --check

Also run the offline scraper whenever Python snapshot generation changes:

python -m scraper --offline --out /tmp/bat-auctions.json
node tools/verify_snapshot.js /tmp/bat-auctions.json

## Working process

Before editing:

1. Read README.md and the files named in the stage prompt.
2. Report the files expected to change.
3. Identify backward-compatibility risks.
4. Implement only the requested stage.
5. Add focused tests.
6. Run the required validation.
7. Report changed files, tests run, manual checks, and unresolved risks.

Do not commit, push, or modify unrelated files.
