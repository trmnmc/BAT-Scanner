// Tests for web/storage.js — run: node --test web/storage.test.js
const test = require("node:test");
const assert = require("node:assert");
const S = require("./storage.js");

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

// --- a watchlist survives "reload" (persists through the backend) -------------------------

test("a saved watchlist survives a reload (new store over the same backend)", () => {
  const be = S.memoryBackend();
  const a = S.createStorage(be, opts());
  a.saveWatchlist({ name: "Cheap trucks", spec: { makes: ["ford"], priceMax: 20000 }, profile_id: "me", origin: "manual" });
  // simulate reload: brand-new store instance over the same persisted backend
  const b = S.createStorage(be, opts());
  const wls = b.listWatchlists("me", false);
  assert.strictEqual(wls.length, 1);
  assert.strictEqual(wls[0].name, "Cheap trucks");
  assert.deepStrictEqual(wls[0].spec, { makes: ["ford"], priceMax: 20000 });
  assert.strictEqual(wls[0].origin, "manual");
});

// --- profile isolation + shared visibility ------------------------------------------------

test("watchlists are profile-isolated; shared ones are visible from either profile", () => {
  const st = S.createStorage(S.memoryBackend(), opts());
  st.saveWatchlist({ name: "Me only", spec: {}, profile_id: "me", origin: "manual" });
  st.saveWatchlist({ name: "Dad only", spec: {}, profile_id: "dad", origin: "manual" });
  st.saveWatchlist({ name: "Shared", spec: {}, profile_id: "shared", origin: "manual" });

  const meNames = st.listWatchlists("me", false).map(w => w.name);
  const dadNames = st.listWatchlists("dad", false).map(w => w.name);
  assert.deepStrictEqual(meNames.sort(), ["Me only", "Shared"]);
  assert.deepStrictEqual(dadNames.sort(), ["Dad only", "Shared"]);
  assert.ok(!meNames.includes("Dad only") && !dadNames.includes("Me only"));
});

test("auction_user_state is profile-isolated; shared state is visible from either profile", () => {
  const st = S.createStorage(S.memoryBackend(), opts());
  st.setUserState("bat:1", "me", { status: "watching", notes: "mine", max_bid: 50000, max_bid_currency: "USD" });
  st.setUserState("bat:1", "dad", { status: "passed", notes: "dad's" });
  st.setUserState("bat:2", "shared", { status: "bid_candidate", notes: "ours" });

  assert.strictEqual(st.getUserState("bat:1", "me").status, "watching");
  assert.strictEqual(st.getUserState("bat:1", "me").notes, "mine");
  assert.strictEqual(st.getUserState("bat:1", "dad").status, "passed");      // isolated
  assert.strictEqual(st.getUserState("bat:1", "dad").notes, "dad's");
  // shared visible from either
  assert.strictEqual(st.resolveUserState("bat:2", "me").status, "bid_candidate");
  assert.strictEqual(st.resolveUserState("bat:2", "dad").status, "bid_candidate");
  // own overrides shared
  st.setUserState("bat:2", "me", { status: "researching" });
  assert.strictEqual(st.resolveUserState("bat:2", "me").status, "researching");
  assert.strictEqual(st.resolveUserState("bat:2", "dad").status, "bid_candidate");
});

test("an invalid status is coerced to none, never stored raw", () => {
  const st = S.createStorage(S.memoryBackend(), opts());
  assert.strictEqual(st.setUserState("bat:9", "me", { status: "bogus" }).status, "none");
});

// --- corrupted storage does not break the app --------------------------------------------

test("corrupted JSON recovers to empty without throwing and without erasing the bad blob", () => {
  const be = S.memoryBackend({
    "bat_watchlists_v1": "{ this is not json",
    "bat_user_state_v1": "also broken]",
  });
  let st;
  assert.doesNotThrow(() => { st = S.createStorage(be, opts()); });
  assert.deepStrictEqual(st.listWatchlists("me", false), []);            // recovered
  assert.strictEqual(st.getUserState("bat:1", "me").status, "none");
  // a subsequent valid write works and the app keeps going
  st.saveWatchlist({ name: "Recovered", spec: {}, profile_id: "me", origin: "manual" });
  assert.strictEqual(st.listWatchlists("me", false).length, 1);
});

// --- existing saved views are preserved + migrated ----------------------------------------

test("legacy saved views migrate to manual watchlists and the legacy key is preserved", () => {
  const be = S.memoryBackend({
    "bat_saved_views_v1": JSON.stringify([
      { name: "Air-cooled", spec: { makes: ["porsche"], yearMax: 1998 } },
      { name: "Cheap", spec: { priceMax: 15000 } },
    ]),
  });
  const st = S.createStorage(be, opts());
  assert.strictEqual(st._migratedCount, 2);
  const names = st.listWatchlists("shared", false).map(w => w.name).sort();
  assert.deepStrictEqual(names, ["Air-cooled", "Cheap"]);
  // legacy key is NOT erased (an old build keeps working)
  assert.ok(be._dump()["bat_saved_views_v1"], "legacy saved-views key preserved");
  // migration is idempotent across reloads
  const st2 = S.createStorage(be, opts());
  assert.strictEqual(st2._migratedCount, 0);
  assert.strictEqual(st2.listWatchlists("shared", false).length, 2);
});

// --- CRUD: enable/disable/rename/delete ---------------------------------------------------

test("enable, disable, rename, and delete a manual watchlist", () => {
  const st = S.createStorage(S.memoryBackend(), opts());
  const wl = st.saveWatchlist({ name: "X", spec: {}, profile_id: "me", origin: "manual", enabled: false });
  assert.strictEqual(wl.enabled, false);
  assert.strictEqual(st.setWatchlistEnabled(wl.id, true).enabled, true);
  assert.strictEqual(st.renameWatchlist(wl.id, "Y").name, "Y");
  assert.strictEqual(st.deleteWatchlist(wl.id), true);
  assert.strictEqual(st.listWatchlists("me", false).length, 0);
});

// --- suggested watchlists -----------------------------------------------------------------

test("the seven suggested watchlists exist as deterministic templates", () => {
  const st = S.createStorage(S.memoryBackend(), opts());
  const sugg = st.suggestedWatchlists();
  const names = sugg.map(w => w.name);
  for (const n of ["Future collectibles", "Quiet auctions", "Investment-grade cars", "Weird specs", "Enthusiast gems", "Below-market candidates", "High-upside driver cars"]) {
    assert.ok(names.includes(n), "missing suggested: " + n);
  }
  assert.ok(sugg.every(w => w.origin === "suggested"));
  // enabling a suggested watchlist persists and survives reload
  const be2 = S.memoryBackend();
  const a = S.createStorage(be2, opts());
  const inv = a.suggestedWatchlists().find(w => w.name === "Investment-grade cars");
  a.setWatchlistEnabled(inv.id, true);
  const b = S.createStorage(be2, opts());
  assert.ok(b.enabledWatchlists("me").some(w => w.id === inv.id), "suggested enabled-state persisted");
});

// --- matching REUSES BATFilters; Radar only on actual matches; reason visible -------------

test("watchlistMatches reuses BATFilters spec matching plus the deterministic predicate", () => {
  const st = S.createStorage(S.memoryBackend(), opts());
  const porsche = { name: "P", spec: { makes: ["porsche"] } };
  assert.strictEqual(st.watchlistMatches(porsche, car(), Date.now()), true);
  assert.strictEqual(st.watchlistMatches(porsche, car({ make: { slug: "ford", name: "Ford" } }), Date.now()), false);

  // suggested "Quiet auctions" uses spec({}) + predicate(comments<=5)
  const quiet = st.suggestedWatchlists().find(w => w.name === "Quiet auctions");
  assert.strictEqual(st.watchlistMatches(quiet, car({ engagement: { comments: 3, watchers: 10 } }), Date.now()), true);
  assert.strictEqual(st.watchlistMatches(quiet, car({ engagement: { comments: 40, watchers: 10 } }), Date.now()), false);
  // missing comments -> not a match (never treated as 0)
  assert.strictEqual(st.watchlistMatches(quiet, car({ engagement: { comments: null, watchers: 10 } }), Date.now()), false);
});

test("matchReason names the watchlist and its constraints (why it matched)", () => {
  const st = S.createStorage(S.memoryBackend(), opts());
  const reason = st.matchReason({ name: "Cheap Porsches", spec: { makes: ["porsche"], priceMax: 30000 } }, car());
  assert.ok(/Cheap Porsches/.test(reason));
  assert.ok(reason.length > "Cheap Porsches".length, "reason includes constraints");
});

// --- active profile persists --------------------------------------------------------------

test("active profile persists and rejects invalid values", () => {
  const be = S.memoryBackend();
  const a = S.createStorage(be, opts());
  assert.strictEqual(a.getActiveProfile(), "me");        // default
  a.setActiveProfile("dad");
  assert.strictEqual(S.createStorage(be, opts()).getActiveProfile(), "dad");   // persisted
  a.setActiveProfile("bogus");
  assert.strictEqual(a.getActiveProfile(), "dad");        // invalid ignored
});

// a backend whose setItem throws for chosen keys (quota / private mode), reads still work
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

// --- review #1 (blocker): migration must NOT mark itself done if the watchlist write fails -------

test("a failed watchlist write during migration does not mark migration done (legacy views not orphaned)", () => {
  const seed = { "bat_saved_views_v1": JSON.stringify([{ name: "Air-cooled", spec: { makes: ["porsche"] } }, { name: "Cheap", spec: { priceMax: 15000 } }]) };
  const be = flakyBackend(seed, ["bat_watchlists_v1"]);   // watchlists write fails; meta write OK
  const a = S.createStorage(be, opts());
  assert.strictEqual(a._migratedCount, 0, "migration reports nothing persisted");
  assert.ok(!(JSON.parse(be._dump()["bat_store_meta_v1"] || "{}").migratedLegacy), "migratedLegacy flag NOT set on failure");
  // quota recovers -> a fresh load over the SAME data migrates exactly once, no duplicates
  const healthy = S.memoryBackend(be._dump());
  const b = S.createStorage(healthy, opts());
  assert.strictEqual(b._migratedCount, 2);
  assert.strictEqual(b.listWatchlists("shared", false).length, 2);
  assert.strictEqual(S.createStorage(healthy, opts())._migratedCount, 0, "idempotent on next load");
});

// --- review #3: mutations return null when the persist fails (so the UI can warn) ---------------

test("saveWatchlist / setUserState return null when the write fails", () => {
  const wlFail = S.createStorage(flakyBackend({}, ["bat_watchlists_v1"]), opts());
  assert.strictEqual(wlFail.saveWatchlist({ name: "X", spec: {}, profile_id: "me", origin: "manual" }), null);
  assert.strictEqual(wlFail.listWatchlists("me", false).length, 0, "nothing persisted");
  const stFail = S.createStorage(flakyBackend({}, ["bat_user_state_v1"]), opts());
  assert.strictEqual(stFail.setUserState("bat:1", "me", { status: "watching" }), null);
});

// --- review #2: an invalid status from storage is coerced to none on READ ----------------------

test("a valid-JSON but invalid status is coerced to none on read (both own + shared)", () => {
  const be = S.memoryBackend({ "bat_user_state_v1": JSON.stringify({
    "me|bat:1": { auction_key: "bat:1", profile_id: "me", status: "PURCHASED_LOL" },
    "shared|bat:2": { auction_key: "bat:2", profile_id: "shared", status: "FUTURE_STATUS" },
  }) });
  const st = S.createStorage(be, opts());
  assert.strictEqual(st.getUserState("bat:1", "me").status, "none");
  assert.strictEqual(st.resolveUserState("bat:1", "me").status, "none");
  assert.strictEqual(st.resolveUserState("bat:2", "me").status, "none");   // shared fallback branch
  // the bad blob is recovered, not rewritten
  assert.ok(/PURCHASED_LOL/.test(be._dump()["bat_user_state_v1"]));
});

// --- review #5: large/foreign fields are never persisted (no large records) --------------------

test("a spec or user-state patch can't smuggle large/extraneous fields into storage", () => {
  const st = S.createStorage(S.memoryBackend(), opts());
  const fatCar = { makes: ["ford"], title: "x".repeat(5000), raw_html: "y".repeat(10000), thumbnail_url: "z".repeat(2000) };
  const wl = st.saveWatchlist({ name: "Fat", spec: fatCar, profile_id: "me", origin: "manual" });
  assert.deepStrictEqual(Object.keys(wl.spec), ["makes"], "only known spec keys persisted");
  const s = st.setUserState("bat:1", "me", { status: "watching", notes: "ok", car: fatCar, bogus: 123 });
  assert.ok(!("car" in s) && !("bogus" in s), "extraneous user-state keys dropped");
});

// --- review #6 (major): high-upside-driver never reads deal_pct on reserve/untrusted bases ------

test("'High-upside driver cars' does not treat a reserve / insufficient-basis deal_pct as upside", () => {
  const st = S.createStorage(S.memoryBackend(), opts());
  const hud = st.suggestedWatchlists().find(w => w.name === "High-upside driver cars");
  const base = { details: { miles: 80000 }, ends_at: "2100-01-01T00:00:00Z", make: { slug: "ford" }, title: "t" };
  // reserve auction with deal_pct -> NOT a match
  assert.strictEqual(st.watchlistMatches(hud, Object.assign({ flags: { no_reserve: false }, value: { deal_pct: 0.3, basis: "make-model-y3" } }, base), Date.now()), false);
  // no-reserve but insufficient basis -> NOT a match
  assert.strictEqual(st.watchlistMatches(hud, Object.assign({ flags: { no_reserve: true }, value: { deal_pct: 0.3, basis: "insufficient" } }, base), Date.now()), false);
  // no-reserve + trusted basis + upside + drivable -> match
  assert.strictEqual(st.watchlistMatches(hud, Object.assign({ flags: { no_reserve: true }, value: { deal_pct: 0.3, basis: "make-model-y7" } }, base), Date.now()), true);
});

// --- review #8: watchlistMatches gates on a live auction when a clock is supplied --------------

test("an ended auction never matches a watchlist when a clock is supplied (Radar is live-only)", () => {
  const st = S.createStorage(S.memoryBackend(), opts());
  const wl = { name: "P", spec: { makes: ["porsche"] } };
  const now = Date.parse("2026-06-23T12:00:00Z");
  const live = { make: { slug: "porsche" }, ends_at: "2026-06-24T00:00:00Z" };
  const ended = { make: { slug: "porsche" }, ends_at: "2026-06-01T00:00:00Z" };
  assert.strictEqual(st.watchlistMatches(wl, live, now), true);
  assert.strictEqual(st.watchlistMatches(wl, ended, now), false);
  // with no clock (spec-only inspection), the live gate is skipped
  assert.strictEqual(st.watchlistMatches(wl, ended), true);
});
