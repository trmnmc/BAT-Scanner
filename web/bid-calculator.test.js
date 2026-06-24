// Tests for web/bid-calculator.js — run: node --test web/bid-calculator.test.js
const test = require("node:test");
const assert = require("node:assert");
const C = require("./bid-calculator.js");

const BAT = "bat-standard";   // 5%, $250 min, $5,000 max

// --- fee rules: percentage / minimum / maximum cap / fixed -----------------------------------

test("percentage fee applies in the mid band", () => {
  // 5% of $40,000 = $2,000 (between the $250 floor and $5,000 cap)
  assert.strictEqual(C.feeFor(40000, BAT), 2000);
});

test("minimum fee floor applies to a small hammer", () => {
  // 5% of $1,000 = $50 -> floored to the $250 minimum
  assert.strictEqual(C.feeFor(1000, BAT), 250);
});

test("maximum fee cap applies to a large hammer", () => {
  // 5% of $200,000 = $10,000 -> capped at $5,000
  assert.strictEqual(C.feeFor(200000, BAT), 5000);
});

test("a fixed fee is added on top of the (possibly clamped) percentage", () => {
  const rule = { id: "doc", percentage: 0.05, min: 250, max: 5000, fixed: 199 };
  assert.strictEqual(C.feeFor(40000, rule), 2000 + 199);
  assert.strictEqual(C.feeFor(1000, rule), 250 + 199);     // min + fixed
  assert.strictEqual(C.feeFor(0, { percentage: 0, min: 0, max: null, fixed: 500 }), 500);  // pure flat
});

test("the 'none' rule charges no buyer fee", () => {
  assert.strictEqual(C.feeFor(40000, "none"), 0);
});

// --- all-in cost + fixed costs ----------------------------------------------------------------

test("all-in cost sums hammer, fee, tax (on hammer+fee), and the fixed add-ons", () => {
  const inputs = { total_budget: 100000, tax_rate: 0.10, fee_rule: BAT,
                   shipping: 1000, title_reg: 500, inspection: 300, repairs: 0, deferred_reserve: 0, contingency: 0 };
  // hammer 50,000: fee 2,500; tax 0.10*(52,500)=5,250; add-ons 1,800 -> 59,550
  assert.strictEqual(C.allInCost(50000, inputs), 50000 + 2500 + 5250 + 1800);
});

test("fixed costs reduce the maximum hammer bid", () => {
  const lean = { total_budget: 60000, tax_rate: 0.08, fee_rule: BAT };
  const heavy = Object.assign({}, lean, { shipping: 2000, repairs: 3000, contingency: 1500 });
  const a = C.calculate(lean), b = C.calculate(heavy);
  assert.ok(a.ok && b.ok);
  assert.ok(b.max_hammer < a.max_hammer, "more fixed costs -> lower affordable hammer");
});

// --- no tax -----------------------------------------------------------------------------------

test("no tax (tax_rate 0) is valid and adds no tax", () => {
  const inputs = { total_budget: 100000, tax_rate: 0, fee_rule: BAT, shipping: 1000 };
  assert.strictEqual(C.allInCost(50000, inputs), 50000 + 2500 + 0 + 1000);
  const r = C.calculate(inputs);
  assert.ok(r.ok && r.errors.length === 0);
});

// --- invalid input + null handling (rule 14) --------------------------------------------------

test("invalid inputs are rejected with errors and null outputs (never a misleading 0)", () => {
  const negBudget = C.calculate({ total_budget: -5, tax_rate: 0.08, fee_rule: BAT });
  assert.strictEqual(negBudget.ok, false);
  assert.ok(negBudget.errors.some(e => /total_budget/.test(e)));
  assert.strictEqual(negBudget.max_hammer, null);

  const nullTax = C.calculate({ total_budget: 50000, tax_rate: null, fee_rule: BAT });
  assert.strictEqual(nullTax.ok, false);            // tax_rate must be explicit — not silently 0
  assert.ok(nullTax.errors.some(e => /tax_rate/.test(e)));

  const badRule = C.calculate({ total_budget: 50000, tax_rate: 0.08, fee_rule: "no-such-rule" });
  assert.ok(!badRule.ok && badRule.errors.some(e => /fee_rule/.test(e)));

  const negShip = C.calculate({ total_budget: 50000, tax_rate: 0.08, fee_rule: BAT, shipping: -100 });
  assert.ok(!negShip.ok && negShip.errors.some(e => /shipping/.test(e)));

  const nanRepairs = C.calculate({ total_budget: 50000, tax_rate: 0.08, fee_rule: BAT, repairs: NaN });
  assert.ok(!nanRepairs.ok && nanRepairs.errors.some(e => /repairs/.test(e)));
});

test("omitted optional add-ons use the documented zero default (not an error)", () => {
  const r = C.calculate({ total_budget: 50000, tax_rate: 0.08, fee_rule: BAT });   // no shipping/repairs/etc
  assert.ok(r.ok && r.errors.length === 0);
  assert.strictEqual(r.fixed_costs, 0);
});

// --- budget too low ---------------------------------------------------------------------------

test("a budget below the unavoidable floor reports budget_too_low and a zero max hammer", () => {
  // shipping 1,000 alone (plus the $250 min fee + tax) already exceeds a $500 budget
  const r = C.calculate({ total_budget: 500, tax_rate: 0.10, fee_rule: BAT, shipping: 1000 });
  assert.ok(r.ok);                       // valid inputs, just an unaffordable budget
  assert.strictEqual(r.max_hammer, 0);
  assert.strictEqual(r.budget_too_low, true);
  assert.strictEqual(r.breakdown.at_max_hammer, null);
});

// --- exact break-even budget ------------------------------------------------------------------

test("a budget set to the exact all-in of a hammer yields that hammer as the maximum", () => {
  const inputs = { total_budget: 0, tax_rate: 0.10, fee_rule: BAT, shipping: 1000 };
  inputs.total_budget = C.allInCost(50000, inputs);   // exact break-even at hammer 50,000
  assert.strictEqual(inputs.total_budget, 58750);     // 50000 + 2500 + 5250 + 1000
  const r = C.calculate(inputs);
  assert.strictEqual(r.max_hammer, 50000);
  assert.strictEqual(r.budget_too_low, false);
});

// --- outputs: room, walk-away, best-case vs conservative --------------------------------------

test("outputs include room, walk-away, and a best-case/conservative bracket", () => {
  const inputs = { total_budget: 80000, tax_rate: 0.08, fee_rule: BAT,
                   shipping: 1500, inspection: 400, deferred_reserve: 2000, contingency: 1500, current_bid: 40000 };
  const r = C.calculate(inputs);
  assert.ok(r.ok);
  assert.strictEqual(r.walk_away, r.max_hammer);
  assert.strictEqual(r.remaining_room, Math.round((r.max_hammer - 40000) * 100) / 100);
  // best case drops the soft buffers (deferred + contingency), so it allows a higher hammer and a lower cost
  assert.ok(r.best_case_max_hammer >= r.conservative_max_hammer);
  assert.ok(r.best_case_estimate < r.conservative_estimate);
  assert.strictEqual(r.all_in_at_bid, C.allInCost(40000, inputs));
});

test("the breakdown's line items always sum to its own all_in total (no penny drift)", () => {
  // pick inputs whose raw tax has a sub-cent fraction that used to round the total away from the parts
  const cases = [
    { total_budget: 25388, tax_rate: 0.05, fee_rule: BAT, shipping: 1181, repairs: 540 },
    { total_budget: 137000, tax_rate: 0.0825, fee_rule: BAT, shipping: 999, contingency: 333 },
    { total_budget: 6499, tax_rate: 0.071, fee_rule: BAT, shipping: 4843 },
  ];
  for (const inputs of cases) {
    const b = C.calculate(inputs).breakdown.at_max_hammer;
    if (!b) continue;
    assert.strictEqual(Math.round((b.hammer + b.fee + b.tax + b.extras) * 100) / 100, b.all_in,
      "line items must add up to all_in for " + JSON.stringify(inputs));
  }
});

test("remaining_room goes negative once the current bid passes your maximum", () => {
  const inputs = { total_budget: 50000, tax_rate: 0.08, fee_rule: BAT, current_bid: 90000 };
  const r = C.calculate(inputs);
  assert.ok(r.remaining_room < 0, "bid above the max -> negative room (walk away)");
});
