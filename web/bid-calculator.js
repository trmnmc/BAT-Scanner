// web/bid-calculator.js — personal maximum-bid + all-in cost calculator (pure, no network, no AI).
//
// Given an all-in budget and the costs around a purchase (buyer fee, tax, shipping, title, PPI,
// repairs, reserves), it computes the highest HAMMER bid that still lands within budget, the
// current all-in cost, the remaining bidding room, and a best-case/conservative bracket. It is a
// PERSONAL planning tool: it reads no market data and never touches Opportunity Score / market
// estimate / badges, never automates bidding, and never sees BaT credentials. Same IIFE + CommonJS
// pattern as the other web/*.js modules.
//
// Money inputs are plain numbers in the auction's own currency. `tax_rate` / a fee `percentage` are
// FRACTIONS (0.08 = 8%). Null handling (spec rule 14): the REQUIRED inputs (total_budget, tax_rate,
// fee_rule) error on null — they never silently become 0. The optional cost add-ons (shipping,
// title_reg, inspection, repairs, deferred_reserve, contingency, fixed_tax) have a DOCUMENTED zero
// default — omitted/null means 0; a *provided* invalid value (negative / NaN / non-number) is an error.
(function (global) {
  "use strict";

  // Configurable fee-rule objects — NOT one hard-coded universal buyer fee (spec rules 1-3). A rule
  // is { id, label, percentage, min, max, fixed }: fee = clamp(percentage*hammer, min, max) + fixed.
  // `min`/`max` bound the PERCENTAGE portion (BaT's "5%, $250 min, $5,000 max"); `fixed` is added on
  // top (a flat doc fee). `max: null` means uncapped. Callers may also pass a custom rule object.
  var FEE_RULES = {
    "bat-standard": { id: "bat-standard", label: "BaT buyer fee (5%, $250–$5,000)", percentage: 0.05, min: 250, max: 5000, fixed: 0 },
    "flat-fixed":   { id: "flat-fixed",   label: "Flat fixed fee",                  percentage: 0,    min: 0,   max: null, fixed: 0 },
    "none":         { id: "none",         label: "No buyer fee",                    percentage: 0,    min: 0,   max: null, fixed: 0 },
    "custom":       { id: "custom",       label: "Custom",                          percentage: 0,    min: 0,   max: null, fixed: 0 },
  };

  // The optional cost add-ons that default to 0 (documented). Kept in one list so the whitelist and
  // the best-case/conservative split stay in sync.
  var COST_FIELDS = ["shipping", "title_reg", "inspection", "repairs", "deferred_reserve", "contingency"];
  // Best case drops the two "soft" buffers (a maybe-later spend), keeping the near-certain costs.
  var SOFT_BUFFERS = ["deferred_reserve", "contingency"];

  function _isNum(v) { return typeof v === "number" && isFinite(v); }
  function _round(v) { return Math.round(v * 100) / 100; }   // cents

  // Normalize a rule (id string -> preset, object -> validated copy). Returns null for an unknown id.
  function normalizeRule(rule) {
    if (typeof rule === "string") rule = FEE_RULES[rule];
    if (!rule || typeof rule !== "object") return null;
    var pct = _isNum(rule.percentage) && rule.percentage >= 0 ? rule.percentage : 0;
    var min = _isNum(rule.min) && rule.min >= 0 ? rule.min : 0;
    var max = _isNum(rule.max) && rule.max >= 0 ? rule.max : null;
    var fixed = _isNum(rule.fixed) && rule.fixed >= 0 ? rule.fixed : 0;
    if (max != null && max < min) max = min;            // a cap below the floor is just the floor
    return { id: typeof rule.id === "string" ? rule.id : "custom", label: rule.label || rule.id || "Custom",
             percentage: pct, min: min, max: max, fixed: fixed };
  }

  // Buyer fee for a given hammer under a rule. clamp(percentage*hammer, min, max) + fixed.
  function feeFor(hammer, rule) {
    var r = normalizeRule(rule);
    if (r == null || !_isNum(hammer) || hammer < 0) return null;
    var pct = r.percentage * hammer;
    if (r.min != null) pct = Math.max(pct, r.min);
    if (r.max != null) pct = Math.min(pct, r.max);
    return _round(pct + r.fixed);
  }

  // The all-in cost at a given hammer, for a validated input set. `extras` lets the caller pass the
  // best-case subset (soft buffers removed); defaults to ALL cost add-ons. Tax applies to the
  // purchase price (hammer + buyer fee) plus any fixed_tax.
  function allInCost(hammer, inputs, extras) {
    if (!_isNum(hammer) || hammer < 0) return null;
    var rule = normalizeRule(inputs.fee_rule);
    if (rule == null || !_isNum(inputs.tax_rate)) return null;
    var fee = feeFor(hammer, rule);
    var tax = inputs.tax_rate * (hammer + fee) + (_isNum(inputs.fixed_tax) ? inputs.fixed_tax : 0);
    var add = extras != null ? extras : _sumCosts(inputs, COST_FIELDS);
    return _round(hammer + fee + tax + add);
  }

  function _sumCosts(inputs, fields) {
    var s = 0;
    for (var i = 0; i < fields.length; i++) {
      var v = inputs[fields[i]];
      if (_isNum(v)) s += v;            // null/undefined -> documented 0 default
    }
    return s;
  }

  // Largest whole-dollar hammer with allInCost(hammer) <= budget. Monotonic in hammer (the piecewise
  // fee is non-decreasing), so a bisection is exact. Returns 0 when even a $0 hammer is unaffordable.
  function _maxHammer(inputs, budget, extras) {
    if (allInCost(0, inputs, extras) > budget) return 0;
    var lo = 0, hi = budget;             // allInCost(budget) > budget, so budget is a safe upper bound
    for (var i = 0; i < 60; i++) {
      var mid = (lo + hi) / 2;
      if (allInCost(mid, inputs, extras) <= budget) lo = mid; else hi = mid;
    }
    return Math.floor(lo);
  }

  // ---- validation (spec rule 14: nulls never silently become zero on a required field) ----
  function _validate(inputs) {
    var errors = [];
    if (!_isObj(inputs)) return ["inputs must be an object"];
    if (!_isNum(inputs.total_budget) || inputs.total_budget <= 0) errors.push("total_budget is required and must be a positive number");
    if (!_isNum(inputs.tax_rate) || inputs.tax_rate < 0) errors.push("tax_rate is required and must be a number >= 0 (use 0 for no tax)");
    if (normalizeRule(inputs.fee_rule) == null) errors.push("fee_rule is required (a known id or a rule object)");
    // optional add-ons: null/omitted is the documented 0 default; a PROVIDED bad value is an error.
    COST_FIELDS.concat(["fixed_tax", "current_bid"]).forEach(function (f) {
      var v = inputs[f];
      if (v == null) return;
      if (!_isNum(v) || v < 0) errors.push(f + " must be a number >= 0 when provided");
    });
    return errors;
  }

  function _isObj(v) { return v != null && typeof v === "object" && !Array.isArray(v); }

  // A self-consistent breakdown: each line item is rounded to cents and `all_in` is the SUM of those
  // rounded items, so the displayed parts always add up to the displayed total (allInCost rounds the
  // raw sum, which can differ by a cent — fine for the solver, confusing in a shown breakdown).
  function _breakdownAt(hammer, inputs, rule, extras) {
    var fee = feeFor(hammer, rule);
    var tax = _round(inputs.tax_rate * (hammer + fee) + (_isNum(inputs.fixed_tax) ? inputs.fixed_tax : 0));
    var ex = _round(extras);
    return { hammer: hammer, fee: fee, tax: tax, extras: ex, all_in: _round(hammer + fee + tax + ex) };
  }

  // The full calculation. Returns { ok, errors, ...outputs }. When invalid, ok:false + errors and all
  // numeric outputs are null (never a misleading 0).
  function calculate(inputs) {
    inputs = inputs || {};
    var errors = _validate(inputs);
    var rule = normalizeRule(inputs.fee_rule);
    var base = {
      ok: false, errors: errors, fee_rule: rule,
      max_hammer: null, walk_away: null, all_in_at_bid: null, remaining_room: null,
      best_case_estimate: null, conservative_estimate: null,
      best_case_max_hammer: null, conservative_max_hammer: null,
      fixed_costs: null, budget_too_low: null, breakdown: null,
    };
    if (errors.length) return base;

    var budget = inputs.total_budget;
    var conservativeExtras = _sumCosts(inputs, COST_FIELDS);
    var bestExtras = _sumCosts(inputs, COST_FIELDS.filter(function (f) { return SOFT_BUFFERS.indexOf(f) === -1; }));

    var maxH = _maxHammer(inputs, budget, conservativeExtras);
    var bestH = _maxHammer(inputs, budget, bestExtras);
    var bid = _isNum(inputs.current_bid) ? inputs.current_bid : null;

    var out = Object.assign({}, base, {
      ok: true,
      fixed_costs: _round(conservativeExtras),
      max_hammer: maxH,                       // the safe ceiling
      walk_away: maxH,                        // bidding above this exceeds the budget
      conservative_max_hammer: maxH,
      best_case_max_hammer: bestH,            // if you skip the soft buffers (deferred + contingency)
      budget_too_low: maxH <= 0,
      breakdown: { at_max_hammer: maxH > 0 ? _breakdownAt(maxH, inputs, rule, conservativeExtras) : null },
    });
    if (bid != null) {
      out.all_in_at_bid = allInCost(bid, inputs, conservativeExtras);          // estimated current all-in (conservative)
      out.conservative_estimate = out.all_in_at_bid;
      out.best_case_estimate = allInCost(bid, inputs, bestExtras);             // optimistic (soft buffers dropped)
      out.remaining_room = _round(maxH - bid);                                 // negative => already above your max
    }
    return out;
  }

  var api = {
    FEE_RULES: FEE_RULES, COST_FIELDS: COST_FIELDS, SOFT_BUFFERS: SOFT_BUFFERS,
    normalizeRule: normalizeRule, feeFor: feeFor, allInCost: allInCost, calculate: calculate,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;  // node tests
  global.BATBidCalc = api;                                                    // browser
})(typeof window !== "undefined" ? window : globalThis);
