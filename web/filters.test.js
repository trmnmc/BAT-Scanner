// Unit tests for the filter engine. Run with: node --test web/filters.test.js
// (Node's built-in runner — zero install. Swap to vitest later if a JS toolchain lands.)
const { test } = require("node:test");
const assert = require("node:assert");
const { matchesFilter, filterCars, knownCount } = require("./filters.js");

function car(over) {
  return Object.assign({
    id: 1, year: 1990, make: { slug: "porsche" }, models: [{ slug: "911" }],
    category_ids: ["air-cooled-911-family"],
    bid: { amount: 50000, currency: "USD", status: "live" },
    flags: { no_reserve: true }, details: null, value: null,
  }, over || {});
}

test("empty spec matches everything", () => {
  assert.equal(matchesFilter(car(), null), true);
  assert.equal(matchesFilter(car(), {}), true);
});

test("make filter", () => {
  assert.equal(matchesFilter(car(), { makes: ["porsche"] }), true);
  assert.equal(matchesFilter(car(), { makes: ["toyota"] }), false);
});

test("era filter (inclusive)", () => {
  assert.equal(matchesFilter(car({ year: 1990 }), { yearMin: 1980, yearMax: 1999 }), true);
  assert.equal(matchesFilter(car({ year: 2005 }), { yearMin: 1980, yearMax: 1999 }), false);
  assert.equal(matchesFilter(car({ year: null }), { yearMin: 1980 }), false); // unknown year fails an explicit year filter
});

test("price filter (car's own currency, not converted)", () => {
  assert.equal(matchesFilter(car({ bid: { amount: 50000 } }), { priceMax: 60000 }), true);
  assert.equal(matchesFilter(car({ bid: { amount: 70000 } }), { priceMax: 60000 }), false);
  assert.equal(matchesFilter(car({ bid: { amount: 5000 } }), { priceMin: 10000 }), false);
});

test("no-reserve filter", () => {
  assert.equal(matchesFilter(car({ flags: { no_reserve: true } }), { noReserve: true }), true);
  assert.equal(matchesFilter(car({ flags: { no_reserve: false } }), { noReserve: true }), false);
});

test("category preset filter", () => {
  assert.equal(matchesFilter(car(), { category: "air-cooled-911-family" }), true);
  assert.equal(matchesFilter(car({ category_ids: [] }), { category: "air-cooled-911-family" }), false);
});

test("dealsOnly keeps no-reserve scoreable deals, drops non-deals, unscored, and reserve", () => {
  const deal = car({ value: { deal_pct: 0.3, basis: "make-model-y3" } });
  const meh = car({ value: { deal_pct: 0.02, basis: "make-model-y3" } });
  const thin = car({ value: { deal_pct: 0.5, basis: "insufficient" } });
  const none = car({ value: null });
  const reserve = car({ flags: { no_reserve: false }, value: { deal_pct: 0.5, basis: "make-model-y3" } });
  assert.equal(matchesFilter(deal, { dealsOnly: true }), true);
  assert.equal(matchesFilter(meh, { dealsOnly: true }), false);
  assert.equal(matchesFilter(thin, { dealsOnly: true }), false); // thin comps are not a deal
  assert.equal(matchesFilter(none, { dealsOnly: true }), false);
  assert.equal(matchesFilter(reserve, { dealsOnly: true }), false); // reserve bid isn't a real price
});

test("milesMax: known-over hidden, unknown PASSES (1C-A)", () => {
  const lowMiles = car({ details: { miles: 30000 } });
  const highMiles = car({ details: { miles: 200000 } });
  const unknown = car({ details: null }); // un-enriched
  assert.equal(matchesFilter(lowMiles, { milesMax: 60000 }), true);
  assert.equal(matchesFilter(highMiles, { milesMax: 60000 }), false);
  assert.equal(matchesFilter(unknown, { milesMax: 60000 }), true); // never hidden for missing data
});

test("excludeConditions: known-flagged hidden, unknown PASSES (1C-A)", () => {
  const restomod = car({ details: { condition: ["restomod"] } });
  const clean = car({ details: { condition: [] } });
  const unknown = car({ details: null });
  assert.equal(matchesFilter(restomod, { excludeConditions: ["restomod", "replica"] }), false);
  assert.equal(matchesFilter(clean, { excludeConditions: ["restomod"] }), true);
  assert.equal(matchesFilter(unknown, { excludeConditions: ["restomod"] }), true);
});

test("filterCars + knownCount", () => {
  const cars = [car({ id: 1, details: { miles: 10000 } }),
                car({ id: 2, details: null }),
                car({ id: 3, make: { slug: "toyota" }, details: { miles: 20000 } })];
  assert.equal(filterCars(cars, { makes: ["porsche"] }).length, 2);
  assert.equal(knownCount(cars, "miles"), 2); // only 2 of 3 have mileage
});

// --- extended (LLM-ready) spec fields ---

const NOW = Date.parse("2026-06-22T00:00:00Z");
const inHours = (h) => new Date(NOW + h * 3600 * 1000).toISOString();

function richCar(over) {
  return Object.assign({
    id: 1, year: 1987, title: "1987 BMW E30 325is 5-Speed Wagon",
    make: { name: "BMW", slug: "bmw" },
    models: [{ name: "E30", slug: "e30" }],
    taxonomy_paths: ["bmw/e30"],
    category_ids: [],
    bid: { amount: 22000, currency: "USD", status: "live" },
    ends_at: inHours(24),
    flags: { no_reserve: false }, details: null, value: null,
  }, over || {});
}

test("makes match is case-insensitive on the slug", () => {
  assert.equal(matchesFilter(richCar(), { makes: ["BMW"] }), true);
  assert.equal(matchesFilter(richCar(), { makes: ["audi"] }), false);
});

test("models match slug / name / title / taxonomy (OR)", () => {
  assert.equal(matchesFilter(richCar(), { models: ["e30"] }), true);          // slug
  assert.equal(matchesFilter(richCar(), { models: ["911", "e30"] }), true);   // OR, one hits
  assert.equal(matchesFilter(richCar(), { models: ["911"] }), false);
  assert.equal(matchesFilter(richCar({ models: [], taxonomy_paths: ["bmw/e30"], title: "1987 BMW" }), { models: ["e30"] }), true); // via taxonomy
});

test("currencies filter (OR, case-insensitive)", () => {
  assert.equal(matchesFilter(richCar({ bid: { amount: 1, currency: "USD" } }), { currencies: ["USD", "EUR"] }), true);
  assert.equal(matchesFilter(richCar({ bid: { amount: 1, currency: "GBP" } }), { currencies: ["USD", "EUR"] }), false);
  assert.equal(matchesFilter(richCar({ bid: { amount: 1, currency: "eur" } }), { currencies: ["EUR"] }), true);
});

test("endingWithinHours needs a valid future ends_at within the window", () => {
  assert.equal(matchesFilter(richCar({ ends_at: inHours(10) }), { endingWithinHours: 48 }, NOW), true);
  assert.equal(matchesFilter(richCar({ ends_at: inHours(72) }), { endingWithinHours: 48 }, NOW), false); // too far
  assert.equal(matchesFilter(richCar({ ends_at: inHours(-1) }), { endingWithinHours: 48 }, NOW), false); // already ended
  assert.equal(matchesFilter(richCar({ ends_at: null }), { endingWithinHours: 48 }, NOW), false);        // no end time
});

test("requiredTerms: ALL must appear in the searchable text", () => {
  assert.equal(matchesFilter(richCar(), { requiredTerms: ["wagon"] }), true);
  assert.equal(matchesFilter(richCar(), { requiredTerms: ["wagon", "5 speed"] }), true); // punctuation-normalized
  assert.equal(matchesFilter(richCar(), { requiredTerms: ["wagon", "diesel"] }), false); // diesel absent
  assert.equal(matchesFilter(richCar(), { requiredTerms: ["bmw"] }), true);              // make name in text
});

test("termGroups: every group is an OR; all groups must hit", () => {
  // group1: body style, group2: transmission
  const spec = { termGroups: [["wagon", "estate", "touring", "avant"], ["manual", "4-speed", "5-speed"]] };
  assert.equal(matchesFilter(richCar(), spec), true);                       // "wagon" + "5-speed"
  assert.equal(matchesFilter(richCar({ title: "1987 BMW E30 325is Sedan 5-Speed" }), spec), false); // no body match
  assert.equal(matchesFilter(richCar({ title: "1987 BMW E30 325is Wagon Automatic" }), spec), false); // no trans match
  assert.equal(matchesFilter(richCar(), { termGroups: [[]] }), true);       // empty group = no constraint
});

test("excludedTerms: any hit rejects", () => {
  assert.equal(matchesFilter(richCar({ title: "1987 BMW E30 Project Wagon" }), { excludedTerms: ["project", "replica"] }), false);
  assert.equal(matchesFilter(richCar(), { excludedTerms: ["project"] }), true);
});

test("combination: make + model + price + term + exclude", () => {
  const spec = { makes: ["bmw"], models: ["e30"], priceMax: 35000,
                 termGroups: [["wagon"]], excludedTerms: ["project"] };
  assert.equal(matchesFilter(richCar(), spec), true);
  assert.equal(matchesFilter(richCar({ bid: { amount: 99000, currency: "USD" } }), spec), false); // price
  assert.equal(matchesFilter(richCar({ title: "1987 BMW E30 Project Wagon" }), spec), false);     // excluded
  assert.equal(matchesFilter(richCar({ make: { slug: "audi" } }), spec), false);                  // make
});

test("buildSearchableText concatenates title + make + models + taxonomy, normalized", () => {
  const { buildSearchableText } = require("./filters.js");
  const t = buildSearchableText(richCar());
  assert.ok(t.includes("bmw"));
  assert.ok(t.includes("e30"));
  assert.ok(t.includes("wagon"));
  assert.ok(t.includes("5 speed"), "punctuation normalized to spaces");
});
