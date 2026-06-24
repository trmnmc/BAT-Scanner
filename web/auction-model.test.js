// Tests for web/auction-model.js — run: node --test web/auction-model.test.js
const test = require("node:test");
const assert = require("node:assert");
const A = require("./auction-model.js");

// A minimal LEGACY live auction — mirrors data/auctions.json and carries NONE of the new
// normalized blocks (no marketplace / historical_status / vehicle_identity / analysis).
function legacyLive(over) {
  return Object.assign({
    id: 115717336,
    title: "1990 Porsche 911 Carrera 2",
    year: 1990,
    make: { id: null, name: "Porsche", slug: "porsche" },
    models: [{ id: null, name: "911", slug: "911" }],
    bid: { amount: 42000, currency: "USD", status: "live" },
    engagement: { comments: 12, views: null, watchers: 300 },
    ends_at: "2026-06-23T18:22:00Z",
    flags: { no_reserve: false, premium: false, alumni: null },
    listing_url: "https://bringatrailer.com/listing/1990-porsche-911/",
    value: { fair_value: 60000, n_comps: 6, basis: "make-model-y3", deal_pct: 0.3, is_deal: false, deal_score: null },
  }, over || {});
}

const SNAP = { schema_version: 1, scraped_at: "2026-06-23T16:03:32Z", source: {} };

// --- auctionKey ---------------------------------------------------------------------------

test("auctionKey builds marketplace-qualified keys", () => {
  assert.strictEqual(A.auctionKey("bat", 115717336), "bat:115717336");
  assert.strictEqual(A.auctionKey("BaT", 1), "bat:1");          // marketplace is normalized lower-case
  assert.strictEqual(A.auctionKey(null, 42), "bat:42");          // defaults to the only marketplace
  assert.strictEqual(A.auctionKey("", 42), "bat:42");
  assert.strictEqual(A.auctionKey("bat", null), null);           // no id -> no key (can't dedupe/enrich)
  assert.strictEqual(A.auctionKey("bat", undefined), null);
  assert.strictEqual(A.auctionKey("bat", ""), null);
  assert.strictEqual(A.auctionKey("bat", 0), "bat:0");           // a real 0 id is keyable
  // unkeyable junk ids -> null (never stringified into a bogus/colliding key)
  assert.strictEqual(A.auctionKey("bat", NaN), null);
  assert.strictEqual(A.auctionKey("bat", Infinity), null);
  assert.strictEqual(A.auctionKey("bat", {}), null);
  assert.strictEqual(A.auctionKey("bat", [1, 2]), null);
});

// --- legacy auction record ----------------------------------------------------------------

test("legacy auction with none of the new fields still normalizes and works", () => {
  const n = A.normalizeLiveAuction(legacyLive(), SNAP);

  // the five normalized fields the model must expose
  assert.strictEqual(n.auction_key, "bat:115717336");
  assert.strictEqual(n.marketplace, "bat");
  assert.strictEqual(n.historical_status, "live");
  assert.ok(n.vehicle_identity, "vehicle_identity present");
  assert.strictEqual(n.analysis, null, "no analysis block on a legacy record -> null (absence is valid)");

  // every existing raw field is preserved untouched
  assert.strictEqual(n.title, "1990 Porsche 911 Carrera 2");
  assert.deepStrictEqual(n.bid, { amount: 42000, currency: "USD", status: "live" });
  assert.deepStrictEqual(n.engagement, { comments: 12, views: null, watchers: 300 });
  assert.ok(n.value && n.value.fair_value === 60000, "legacy value block preserved");

  // identity is derived from the legacy make/models, not invented
  assert.strictEqual(n.vehicle_identity.year, 1990);
  assert.strictEqual(n.vehicle_identity.make.slug, "porsche");
  assert.strictEqual(n.vehicle_identity.model.slug, "911");
  assert.strictEqual(n.vehicle_identity.source, "legacy");
  assert.strictEqual(n.vehicle_identity.ambiguous, false);

  // normalization is pure — the input is not mutated
  assert.strictEqual("auction_key" in legacyLive(), false);
});

test("legacy auction missing year/make/model is flagged ambiguous, never invented", () => {
  const n = A.normalizeLiveAuction({ id: 5, title: "mystery lot" }, SNAP);
  assert.strictEqual(n.vehicle_identity.year, null);
  assert.strictEqual(n.vehicle_identity.make, null);
  assert.strictEqual(n.vehicle_identity.model, null);
  assert.strictEqual(n.vehicle_identity.source, null);
  assert.strictEqual(n.vehicle_identity.ambiguous, true);
  assert.strictEqual(n.analysis, null);
  assert.strictEqual(n.auction_key, "bat:5");
});

// --- malformed optional analysis ----------------------------------------------------------

test("malformed optional analysis is handled gracefully (no throw, never a fake zero)", () => {
  for (const bad of ["oops", 42, [], true, NaN]) {
    const n = A.normalizeLiveAuction(legacyLive({ analysis: bad }), SNAP);
    assert.strictEqual(n.analysis, null, "malformed analysis block -> null");
  }
  // a present-but-sparse analysis keeps its real values and nulls the rest
  const a = A.normalizeAnalysis({ score: 0.42, confidence: "low", junk: 1 });
  assert.strictEqual(a.score, 0.42);
  assert.strictEqual(a.confidence, "low");
  assert.strictEqual(a.summary, null);
  assert.strictEqual(a.flags, null);
});

// --- malformed optional vehicle_identity --------------------------------------------------

test("malformed optional vehicle_identity falls back to the legacy identity", () => {
  for (const bad of ["oops", 42, [], true]) {
    const n = A.normalizeLiveAuction(legacyLive({ vehicle_identity: bad }), SNAP);
    assert.strictEqual(n.vehicle_identity.year, 1990, "garbage identity ignored; legacy year recovered");
    assert.strictEqual(n.vehicle_identity.make.slug, "porsche");
    assert.strictEqual(n.vehicle_identity.source, "legacy");
  }
});

test("a well-formed explicit vehicle_identity wins over the legacy fields", () => {
  const n = A.normalizeLiveAuction(
    legacyLive({ vehicle_identity: { year: 1973, make: "porsche", model: { slug: "911", name: "911" }, vin: "ABC123" } }),
    SNAP,
  );
  assert.strictEqual(n.vehicle_identity.year, 1973);
  assert.strictEqual(n.vehicle_identity.vin, "ABC123");
  assert.strictEqual(n.vehicle_identity.source, "explicit");
  assert.strictEqual(n.vehicle_identity.ambiguous, false);
});

// --- historical comp record ---------------------------------------------------------------

test("historical comp record normalizes to sold with a derived identity", () => {
  // a comp's make/model are bare slug strings and it has price/sold_ts, not bid/ends_at. We attach
  // an analysis block on purpose: a settled SALE must never surface a confident score, so the model
  // forces analysis to null by code — not merely because the fixture happened to omit it.
  const comp = { id: 99887766, title: "1973 Porsche 911", make: "porsche", model: "911", year: 1973, price: 58000, sold_ts: 1700000000, category_ids: [], analysis: { score: 0.95, summary: "steal" } };
  const n = A.normalizeHistoricalComp(comp);

  assert.strictEqual(n.auction_key, "bat:99887766");
  assert.strictEqual(n.marketplace, "bat");
  assert.strictEqual(n.historical_status, "sold");
  assert.strictEqual(n.vehicle_identity.year, 1973);
  assert.strictEqual(n.vehicle_identity.make.slug, "porsche");
  assert.strictEqual(n.vehicle_identity.model.slug, "911");
  assert.strictEqual(n.vehicle_identity.ambiguous, false);
  assert.strictEqual(n.analysis, null, "a comp never surfaces analysis, even if one is attached upstream");

  // raw comp fields preserved
  assert.strictEqual(n.price, 58000);
  assert.strictEqual(n.sold_ts, 1700000000);
});

test("historical_status is authoritative by construction — an untrusted raw value can't flip it", () => {
  // a live reserve auction mislabeled "sold" must NOT be relabeled (it would feed a false sale price)
  const live = A.normalizeLiveAuction(legacyLive({ historical_status: "sold" }), SNAP);
  assert.strictEqual(live.historical_status, "live");
  // and a comp can't claim to be live
  const comp = A.normalizeHistoricalComp({ id: 7, make: "ford", model: "bronco", year: 1974, price: 30000, historical_status: "live" });
  assert.strictEqual(comp.historical_status, "sold");
});

// --- null score is not zero ---------------------------------------------------------------

test("a null/absent score stays null and is never coerced to zero", () => {
  assert.strictEqual(A.normalizeAnalysis({ score: null }).score, null);
  assert.strictEqual(A.normalizeAnalysis({}).score, null, "absent score -> null");
  assert.strictEqual(A.normalizeAnalysis({ score: undefined }).score, null);
  assert.strictEqual(A.normalizeAnalysis({ score: "0.5" }).score, null, "non-numeric score -> null");
  assert.strictEqual(A.normalizeAnalysis({ score: Infinity }).score, null, "non-finite score -> null");
  assert.strictEqual(A.normalizeAnalysis({ score: NaN }).score, null);
  assert.strictEqual(A.normalizeAnalysis({ score: 0 }).score, 0, "a real 0 is a value, not missing");

  // and through the full live normalization
  const n = A.normalizeLiveAuction(legacyLive({ analysis: { score: null } }), SNAP);
  assert.strictEqual(n.analysis.score, null);
});
