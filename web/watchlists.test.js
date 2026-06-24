// Tests for web/watchlists.js — run: node --test web/watchlists.test.js
const test = require("node:test");
const assert = require("node:assert");
const W = require("./watchlists.js");
const US = require("./user-state.js");   // store-core / backend (shared persistence layer)

let _t = 1000;
const opts = () => ({ now: () => (_t += 1), genId: () => "id_" + _t });

function car(over) {
  return Object.assign({
    id: 1, title: "1990 Porsche 911", year: 1990,
    make: { name: "Porsche", slug: "porsche" }, models: [{ name: "911", slug: "911" }],
    bid: { amount: 42000, currency: "USD", status: "live" },
    engagement: { comments: 30, watchers: 250 }, flags: { no_reserve: true },
    ends_at: "2100-01-01T00:00:00Z", details: { miles: 60000, condition: [] },
    value: { fair_value: 60000, n_comps: 8, basis: "make-model-y3", deal_pct: 0.2, is_deal: false },
  }, over || {});
}

// a backend whose setItem throws for chosen keys (quota / private mode); reads still work.
function flakyBackend(seed, failKeys) {
  const m = Object.assign({}, seed || {});
  const fail = new Set(failKeys || []);
  return {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null),
    setItem: (k, v) => { if (fail.has(k)) throw new Error("QuotaExceeded"); m[k] = String(v); },
    removeItem: (k) => { delete m[k]; },
    _dump: () => Object.assign({}, m),
  };
}

// --- a watchlist survives "reload" + carries the new field shape ---------------------------

test("a saved watchlist survives a reload and uses the new field shape (profile/source/original_query)", () => {
  const be = US.memoryBackend();
  const a = W.createWatchlists(be, opts());
  a.saveWatchlist({ name: "Cheap trucks", spec: { makes: ["ford"], priceMax: 20000 }, profile: "me", source: "manual", original_query: "ford under $20k" });
  const b = W.createWatchlists(be, opts());            // simulate reload over the same backend
  const wls = b.listWatchlists("me", false);
  assert.strictEqual(wls.length, 1);
  assert.strictEqual(wls[0].name, "Cheap trucks");
  assert.deepStrictEqual(wls[0].spec, { makes: ["ford"], priceMax: 20000 });
  assert.strictEqual(wls[0].source, "manual");
  assert.strictEqual(wls[0].profile, "me");
  assert.strictEqual(wls[0].original_query, "ford under $20k");
});

test("a watchlist persisted in the OLD shape (profile_id/origin) still loads after the refactor", () => {
  const be = US.memoryBackend({ "bat_watchlists_v1": JSON.stringify([
    { id: "old1", schema_version: 1, name: "Legacy", profile_id: "dad", origin: "manual", enabled: true, spec: { makes: ["bmw"] } },
  ]) });
  const wls = W.createWatchlists(be, opts()).listWatchlists("dad", false);
  assert.strictEqual(wls.length, 1);
  assert.strictEqual(wls[0].profile, "dad");      // coerced from profile_id
  assert.strictEqual(wls[0].source, "manual");    // coerced from origin
});

// --- profile isolation + shared visibility -------------------------------------------------

test("watchlists are profile-isolated; shared ones are visible from either profile", () => {
  const st = W.createWatchlists(US.memoryBackend(), opts());
  st.saveWatchlist({ name: "Me only", spec: {}, profile: "me", source: "manual" });
  st.saveWatchlist({ name: "Dad only", spec: {}, profile: "dad", source: "manual" });
  st.saveWatchlist({ name: "Shared", spec: {}, profile: "shared", source: "manual" });

  const meNames = st.listWatchlists("me", false).map(w => w.name).sort();
  const dadNames = st.listWatchlists("dad", false).map(w => w.name).sort();
  assert.deepStrictEqual(meNames, ["Me only", "Shared"]);
  assert.deepStrictEqual(dadNames, ["Dad only", "Shared"]);
});

// --- corruption recovery -------------------------------------------------------------------

test("corrupted watchlist JSON recovers to empty without throwing and without erasing the bad blob", () => {
  const be = US.memoryBackend({ "bat_watchlists_v1": "{ this is not json" });
  let st;
  assert.doesNotThrow(() => { st = W.createWatchlists(be, opts()); });
  assert.deepStrictEqual(st.listWatchlists("me", false), []);            // recovered
  st.saveWatchlist({ name: "Recovered", spec: {}, profile: "me", source: "manual" });
  assert.strictEqual(st.listWatchlists("me", false).length, 1);
});

// --- legacy saved-views migration ----------------------------------------------------------

test("legacy saved views migrate to manual watchlists and the legacy key is preserved", () => {
  const be = US.memoryBackend({
    "bat_saved_views_v1": JSON.stringify([
      { name: "Air-cooled", spec: { makes: ["porsche"], yearMax: 1998 } },
      { name: "Cheap", spec: { priceMax: 15000 } },
    ]),
  });
  const st = W.createWatchlists(be, opts());
  assert.strictEqual(st._migratedCount, 2);
  const names = st.listWatchlists("shared", false).map(w => w.name).sort();
  assert.deepStrictEqual(names, ["Air-cooled", "Cheap"]);
  assert.ok(be._dump()["bat_saved_views_v1"], "legacy saved-views key preserved (never erased)");
  // migration is idempotent across reloads
  assert.strictEqual(W.createWatchlists(be, opts())._migratedCount, 0);
  assert.strictEqual(W.createWatchlists(be, opts()).listWatchlists("shared", false).length, 2);
});

test("a failed watchlist write during migration does not mark migration done (legacy views not orphaned)", () => {
  const seed = { "bat_saved_views_v1": JSON.stringify([{ name: "Air-cooled", spec: { makes: ["porsche"] } }, { name: "Cheap", spec: { priceMax: 15000 } }]) };
  const be = flakyBackend(seed, ["bat_watchlists_v1"]);   // watchlists write fails; meta write OK
  const a = W.createWatchlists(be, opts());
  assert.strictEqual(a._migratedCount, 0, "migration reports nothing persisted");
  assert.ok(!(JSON.parse(be._dump()["bat_store_meta_v1"] || "{}").migratedLegacy), "migratedLegacy flag NOT set on failure");
  const healthy = US.memoryBackend(be._dump());
  assert.strictEqual(W.createWatchlists(healthy, opts())._migratedCount, 2);
  assert.strictEqual(W.createWatchlists(healthy, opts())._migratedCount, 0, "idempotent on next load");
});

// --- CRUD ----------------------------------------------------------------------------------

test("enable, disable, rename, and delete a manual watchlist", () => {
  const st = W.createWatchlists(US.memoryBackend(), opts());
  const wl = st.saveWatchlist({ name: "X", spec: {}, profile: "me", source: "manual", enabled: false });
  assert.strictEqual(wl.enabled, false);
  assert.strictEqual(st.setWatchlistEnabled(wl.id, true).enabled, true);
  assert.strictEqual(st.renameWatchlist(wl.id, "Y").name, "Y");
  assert.strictEqual(st.deleteWatchlist(wl.id), true);
  assert.strictEqual(st.listWatchlists("me", false).length, 0);
});

test("saveWatchlist returns null when the write fails", () => {
  const wlFail = W.createWatchlists(flakyBackend({}, ["bat_watchlists_v1"]), opts());
  assert.strictEqual(wlFail.saveWatchlist({ name: "X", spec: {}, profile: "me", source: "manual" }), null);
  assert.strictEqual(wlFail.listWatchlists("me", false).length, 0, "nothing persisted");
});

test("a spec can't smuggle large/extraneous fields into a watchlist", () => {
  const st = W.createWatchlists(US.memoryBackend(), opts());
  const fatCar = { makes: ["ford"], title: "x".repeat(5000), raw_html: "y".repeat(10000), thumbnail_url: "z".repeat(2000) };
  const wl = st.saveWatchlist({ name: "Fat", spec: fatCar, profile: "me", source: "manual" });
  assert.deepStrictEqual(Object.keys(wl.spec), ["makes"], "only known spec keys persisted");
});

// --- suggested watchlists ------------------------------------------------------------------

test("the seven suggested watchlists exist as deterministic templates", () => {
  const st = W.createWatchlists(US.memoryBackend(), opts());
  const names = st.suggestedWatchlists().map(w => w.name);
  for (const n of ["Future collectibles", "Quiet auctions", "Investment-grade cars", "Weird specs", "Enthusiast gems", "Below-market candidates", "High-upside driver cars"]) {
    assert.ok(names.includes(n), "missing suggested: " + n);
  }
  assert.ok(st.suggestedWatchlists().every(w => w.source === "suggested"));
  // enabling a suggested watchlist persists and survives reload
  const be2 = US.memoryBackend();
  const a = W.createWatchlists(be2, opts());
  const inv = a.suggestedWatchlists().find(w => w.name === "Investment-grade cars");
  a.setWatchlistEnabled(inv.id, true);
  assert.ok(W.createWatchlists(be2, opts()).enabledWatchlists("me").some(w => w.id === inv.id), "suggested enabled-state persisted");
});

// --- matching REUSES BATFilters; Radar only on actual matches; reason visible ---------------

test("watchlistMatches reuses BATFilters spec matching plus the deterministic predicate", () => {
  const st = W.createWatchlists(US.memoryBackend(), opts());
  const porsche = { name: "P", spec: { makes: ["porsche"] } };
  assert.strictEqual(st.watchlistMatches(porsche, car(), Date.now()), true);
  assert.strictEqual(st.watchlistMatches(porsche, car({ make: { slug: "ford", name: "Ford" } }), Date.now()), false);

  const quiet = st.suggestedWatchlists().find(w => w.name === "Quiet auctions");
  assert.strictEqual(st.watchlistMatches(quiet, car({ engagement: { comments: 3, watchers: 10 } }), Date.now()), true);
  assert.strictEqual(st.watchlistMatches(quiet, car({ engagement: { comments: 40, watchers: 10 } }), Date.now()), false);
  // missing comments -> not a match (never treated as 0)
  assert.strictEqual(st.watchlistMatches(quiet, car({ engagement: { comments: null, watchers: 10 } }), Date.now()), false);
});

test("matchReason names the watchlist and its constraints (why it matched)", () => {
  const st = W.createWatchlists(US.memoryBackend(), opts());
  const reason = st.matchReason({ name: "Cheap Porsches", spec: { makes: ["porsche"], priceMax: 30000 } }, car());
  assert.ok(/Cheap Porsches/.test(reason));
  assert.ok(reason.length > "Cheap Porsches".length, "reason includes constraints");
});

test("'High-upside driver cars' does not treat a reserve / insufficient-basis deal_pct as upside", () => {
  const st = W.createWatchlists(US.memoryBackend(), opts());
  const hud = st.suggestedWatchlists().find(w => w.name === "High-upside driver cars");
  const base = { details: { miles: 80000 }, ends_at: "2100-01-01T00:00:00Z", make: { slug: "ford" }, title: "t" };
  assert.strictEqual(st.watchlistMatches(hud, Object.assign({ flags: { no_reserve: false }, value: { deal_pct: 0.3, basis: "make-model-y3" } }, base), Date.now()), false);
  assert.strictEqual(st.watchlistMatches(hud, Object.assign({ flags: { no_reserve: true }, value: { deal_pct: 0.3, basis: "insufficient" } }, base), Date.now()), false);
  assert.strictEqual(st.watchlistMatches(hud, Object.assign({ flags: { no_reserve: true }, value: { deal_pct: 0.3, basis: "make-model-y7" } }, base), Date.now()), true);
});

test("an ended auction never matches a watchlist when a clock is supplied (Radar is live-only)", () => {
  const st = W.createWatchlists(US.memoryBackend(), opts());
  const wl = { name: "P", spec: { makes: ["porsche"] } };
  const now = Date.parse("2026-06-23T12:00:00Z");
  const live = { make: { slug: "porsche" }, ends_at: "2026-06-24T00:00:00Z" };
  const ended = { make: { slug: "porsche" }, ends_at: "2026-06-01T00:00:00Z" };
  assert.strictEqual(st.watchlistMatches(wl, live, now), true);
  assert.strictEqual(st.watchlistMatches(wl, ended, now), false);
  assert.strictEqual(st.watchlistMatches(wl, ended), true);   // no clock -> spec-only inspection
});

// --- deterministic phrase parser (NO AI) ---------------------------------------------------

test("parsePhrase: the five required phrases produce the expected structured rules", () => {
  // 1. Porsche under $100k
  let s = W.parsePhrase("Porsche under $100k");
  assert.deepStrictEqual(s.makes, ["porsche"]);
  assert.strictEqual(s.priceMax, 100000);
  assert.deepStrictEqual(Object.keys(s).sort(), ["makes", "priceMax"]);

  // 2. air-cooled 911
  s = W.parsePhrase("air-cooled 911");
  assert.deepStrictEqual(s.makes, ["porsche"]);     // air-cooled implies Porsche when no make is named
  assert.deepStrictEqual(s.models, ["911"]);
  assert.strictEqual(s.yearMax, 1998);              // air-cooled era cap

  // 3. V8 Mercedes
  s = W.parsePhrase("V8 Mercedes");
  assert.deepStrictEqual(s.makes, ["mercedes-benz"]);
  assert.ok(s.termGroups && s.termGroups.some(g => g.includes("v8")), "engine term -> OR group");

  // 4. Japanese sports cars
  s = W.parsePhrase("Japanese sports cars");
  assert.ok(s.makes.includes("toyota") && s.makes.includes("nissan") && s.makes.includes("honda"));
  assert.deepStrictEqual(Object.keys(s), ["makes"], "'sports cars' are stopwords -> only the make group");

  // 5. no reserve ending within 24 hours
  s = W.parsePhrase("no reserve ending within 24 hours");
  assert.strictEqual(s.noReserve, true);
  assert.strictEqual(s.endingWithinHours, 24);
});

test("parsePhrase: 'over $X' is a floor, days convert to hours, leftover words become keyword terms", () => {
  let s = W.parsePhrase("BMW over $50k");
  assert.strictEqual(s.priceMin, 50000);
  assert.deepStrictEqual(s.makes, ["bmw"]);
  s = W.parsePhrase("no reserve ending within 2 days");
  assert.strictEqual(s.endingWithinHours, 48);
  s = W.parsePhrase("porsche wagon");
  assert.deepStrictEqual(s.makes, ["porsche"]);
  assert.ok(s.requiredTerms && s.requiredTerms.includes("wagon"), "unrecognized word -> keyword AND");
});

test("parsePhrase: hyphenated + multi-word forms never leak into requiredTerms (review fixes)", () => {
  // hyphenated structural tokens the splitter keeps intact must NOT become keyword terms
  assert.deepStrictEqual(W.parsePhrase("no-reserve"), { noReserve: true });
  let s = W.parsePhrase("no-reserve porsche");
  assert.deepStrictEqual(s.makes, ["porsche"]);
  assert.strictEqual(s.noReserve, true);
  assert.ok(!s.requiredTerms, "no leaked 'no-reserve' term");

  // hyphenated + spaced engine terms -> termGroup only, no spurious required term
  for (const phrase of ["v-8 mercedes", "v-12 ferrari", "flat-six 911", "inline-6 bmw", "straight six datsun"]) {
    const r = W.parsePhrase(phrase);
    assert.ok(r.termGroups && r.termGroups.length, "engine OR-group present for: " + phrase);
    assert.ok(!r.requiredTerms, "no leaked engine token for: " + phrase + " -> " + JSON.stringify(r.requiredTerms));
  }
  assert.deepStrictEqual(W.parsePhrase("v-8 mercedes").makes, ["mercedes-benz"]);

  // space-separated multi-word makes resolve to the canonical slug (no "land"/"rover" leak)
  s = W.parsePhrase("land rover defender");
  assert.deepStrictEqual(s.makes, ["land-rover"]);
  assert.ok(!s.requiredTerms, "multi-word make fully consumed");
  assert.deepStrictEqual(W.parsePhrase("rolls royce").makes, ["rolls-royce"]);
  assert.deepStrictEqual(W.parsePhrase("range rover").makes, ["land-rover"]);

  // a genuinely unrecognized hyphenated keyword still decomposes into useful terms
  assert.deepStrictEqual(W.parsePhrase("porsche wide-body").requiredTerms, ["wide", "body"]);
});

test("parsePhrase: an empty / junk phrase yields an empty spec (matches the whole board)", () => {
  assert.deepStrictEqual(W.parsePhrase(""), {});
  assert.deepStrictEqual(W.parsePhrase("   "), {});
  assert.deepStrictEqual(W.parsePhrase(null), {});
});

// a parsed phrase is a usable watchlist spec end-to-end (parse -> save -> match through BATFilters)
test("a watchlist saved from a parsed phrase matches the right live cars", () => {
  const st = W.createWatchlists(US.memoryBackend(), opts());
  const spec = W.parsePhrase("Porsche under $100k");
  const wl = st.saveWatchlist({ name: "Cheap Porsche", spec, profile: "me", source: "manual", original_query: "Porsche under $100k" });
  assert.strictEqual(wl.original_query, "Porsche under $100k");
  const now = Date.parse("2026-06-23T12:00:00Z");
  const cheap = car({ ends_at: "2026-06-24T00:00:00Z", bid: { amount: 80000, currency: "USD" } });
  const pricey = car({ ends_at: "2026-06-24T00:00:00Z", bid: { amount: 180000, currency: "USD" } });
  assert.strictEqual(st.watchlistMatches(wl, cheap, now), true);
  assert.strictEqual(st.watchlistMatches(wl, pricey, now), false);
});
