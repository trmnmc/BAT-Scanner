// Tests for web/comps.js (+ historical-vs-live normalization) — run: node --test web/comps.test.js
const test = require("node:test");
const assert = require("node:assert");
const C = require("./comps.js");
const A = require("./auction-model.js");

// a valid comp row, matching data/comps.json shape
function comp(over) {
  return Object.assign({
    id: 115117526,
    title: "347 Stroker-Powered 1974 Ford Bronco Ranger",
    make: "ford", model: "bronco", year: 1974, price: 76500,
    sold_ts: 1781980318, category_ids: ["vintage-trucks"],
  }, over || {});
}

// --- valid historical comp ---------------------------------------------------------------

test("a valid comp becomes a plot point with real price + year (nothing invented)", () => {
  const p = C.compPoint(comp());
  assert.strictEqual(C.isValidComp(comp()), true);
  assert.strictEqual(p.x, 76500);
  assert.strictEqual(p.y, 1974);
  assert.strictEqual(p.comp.title, "347 Stroker-Powered 1974 Ford Bronco Ranger");
  assert.ok(p.soldMs > 0, "sold_ts seconds -> ms");
  assert.strictEqual(C.compSoldDate(comp()) instanceof Date, true);
});

// --- missing price -----------------------------------------------------------------------

test("a comp with missing/zero/garbage price is skipped, not faked to 0", () => {
  for (const bad of [undefined, null, 0, -5, "76500", NaN, Infinity]) {
    assert.strictEqual(C.isValidComp(comp({ price: bad })), false, String(bad));
    assert.strictEqual(C.compPoint(comp({ price: bad })), null);
  }
});

// --- missing year ------------------------------------------------------------------------

test("a comp with missing/garbage year is skipped, not faked", () => {
  for (const bad of [undefined, null, "1974", NaN, 0, 1700, 2200]) {
    assert.strictEqual(C.isValidComp(comp({ year: bad })), false, String(bad));
    assert.strictEqual(C.compPoint(comp({ year: bad })), null);
  }
});

// --- malformed comp ----------------------------------------------------------------------

test("a malformed comp row is skipped without throwing", () => {
  for (const bad of [null, undefined, 42, "nope", [], { foo: 1 }]) {
    assert.strictEqual(C.isValidComp(bad), false);
    assert.strictEqual(C.compPoint(bad), null);
  }
  // a mixed list keeps the good rows and drops the bad ones
  const pts = C.buildCompPoints([comp(), null, comp({ price: 0 }), "junk", comp({ year: null }), comp({ id: 2 })]);
  assert.strictEqual(pts.length, 2, "two valid comps survive, three bad rows skipped");
});

// --- empty comp list ---------------------------------------------------------------------

test("a missing / empty / non-array comp list yields [] (page keeps working)", () => {
  assert.deepStrictEqual(C.buildCompPoints([]), []);
  assert.deepStrictEqual(C.buildCompPoints(null), []);
  assert.deepStrictEqual(C.buildCompPoints(undefined), []);
  assert.deepStrictEqual(C.buildCompPoints({ comps: [comp()] }), [], "object, not array -> []");
});

// --- sold date when available ------------------------------------------------------------

test("compSoldMs returns null for an absent/garbage sold_ts (date never invented)", () => {
  assert.strictEqual(C.compSoldMs(comp({ sold_ts: null })), null);
  assert.strictEqual(C.compSoldMs(comp({ sold_ts: "yesterday" })), null);
  assert.strictEqual(C.compSoldDate(comp({ sold_ts: null })), null);
  assert.ok(C.compSoldMs(comp({ sold_ts: 1781980318 })) === 1781980318 * 1000);
});

// --- narrowing historical dots by spec (rule 9) ------------------------------------------

test("compMatchesSpec narrows comps by make / model / year / price / keyword", () => {
  const c = comp();   // ford bronco, 1974, $76,500
  assert.strictEqual(C.compMatchesSpec(c, {}), true, "empty spec keeps the comp");
  assert.strictEqual(C.compMatchesSpec(c, { makes: ["ford"] }), true);
  assert.strictEqual(C.compMatchesSpec(c, { makes: ["porsche"] }), false);
  assert.strictEqual(C.compMatchesSpec(c, { models: ["bronco"] }), true);
  assert.strictEqual(C.compMatchesSpec(c, { models: ["911"] }), false);
  assert.strictEqual(C.compMatchesSpec(c, { yearMin: 1980 }), false);
  assert.strictEqual(C.compMatchesSpec(c, { yearMin: 1970, yearMax: 1980 }), true);
  assert.strictEqual(C.compMatchesSpec(c, { priceMax: 50000 }), false);
  assert.strictEqual(C.compMatchesSpec(c, { requiredTerms: ["stroker"] }), true);
  assert.strictEqual(C.compMatchesSpec(c, { excludedTerms: ["bronco"] }), false);

  const ptsFord = C.filterComps([comp(), comp({ make: "porsche", model: "911", title: "1990 Porsche 911" })], { makes: ["ford"] });
  assert.strictEqual(ptsFord.length, 1);
  assert.strictEqual(ptsFord[0].comp.make, "ford");
});

// --- historical vs live record normalization ---------------------------------------------

test("historical comp vs live auction normalize to distinguishable statuses", () => {
  const histComp = comp();
  const hist = A.normalizeHistoricalComp(histComp);
  assert.strictEqual(hist.historical_status, "sold");
  assert.strictEqual(hist.vehicle_identity.make.slug, "ford");
  assert.strictEqual(hist.vehicle_identity.year, 1974);
  assert.strictEqual(hist.analysis, null, "a comp never carries analysis");

  const live = A.normalizeLiveAuction({ id: 9, year: 1990, make: { slug: "porsche", name: "Porsche" }, models: [{ slug: "911", name: "911" }], bid: { amount: 42000, status: "live" } }, {});
  assert.strictEqual(live.historical_status, "live");

  // the two are cleanly distinguishable on the unified field
  assert.notStrictEqual(hist.historical_status, live.historical_status);
});
