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
