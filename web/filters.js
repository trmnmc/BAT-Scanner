// web/filters.js — ONE filter spec, ONE predicate (pivot 2026-06-21).
// The map, the saved-filter chips, and (Phase 2) the LLM "category" box all produce the
// same spec and run through matchesFilter, so the LLM is a natural-language mouth on this
// engine, not a second system.
//
// spec shape (every field optional; omitted = no constraint):
//   { makes: ["porsche", ...],         // car.make.slug in this list
//     yearMin, yearMax,                // inclusive era
//     priceMin, priceMax,              // current bid, in the car's own currency (not converted)
//     noReserve: true,                 // only no-reserve auctions
//     dealsOnly: true,                 // only cars priced under comps (scoreable + deal_pct >= margin)
//     milesMax: 60000,                 // enrichment-only; UNKNOWN mileage PASSES (1C-A)
//     excludeConditions: ["restomod"], // enrichment-only; unknown condition PASSES (1C-A)
//     category: "vintage-trucks" }     // optional preset: car tagged with this category_id
//
// Honesty rule (1C-A): filters that need per-listing enrichment (mileage, condition) never
// hide a car whose data we haven't fetched — unknown passes through, so you never lose a
// car to missing data, only to data that actually fails the filter.
(function (global) {
  "use strict";

  var DEAL_FILTER_MARGIN = 0.15; // "deals only" = at least this far under comps
  var SCOREABLE = { "make-model-y3": 1, "make-model-y7": 1 };

  function isScoreableDeal(v) {
    return !!(v && v.deal_pct != null && SCOREABLE[v.basis] && v.deal_pct >= DEAL_FILTER_MARGIN);
  }

  function matchesFilter(car, spec) {
    if (!spec) return true;
    var bid = car.bid && car.bid.amount;

    if (spec.priceMin != null && (bid == null || bid < spec.priceMin)) return false;
    if (spec.priceMax != null && (bid == null || bid > spec.priceMax)) return false;

    var y = car.year;
    if (spec.yearMin != null && (y == null || y < spec.yearMin)) return false;
    if (spec.yearMax != null && (y == null || y > spec.yearMax)) return false;

    if (spec.makes && spec.makes.length) {
      var mk = car.make && car.make.slug;
      if (!mk || spec.makes.indexOf(mk) === -1) return false;
    }

    if (spec.category) {
      if (!car.category_ids || car.category_ids.indexOf(spec.category) === -1) return false;
    }

    if (spec.noReserve && !(car.flags && car.flags.no_reserve)) return false;

    // "deals only" = under comps AND no-reserve (a reserve bid isn't a real price). Non-deals,
    // unscored, and reserve cars are excluded.
    if (spec.dealsOnly) {
      var nr = car.flags && car.flags.no_reserve;
      if (!nr || !isScoreableDeal(car.value)) return false;
    }

    // mileage: enrichment-only. Known-and-over => hide; unknown => keep (1C-A).
    if (spec.milesMax != null) {
      var miles = car.details && car.details.miles;
      if (miles != null && miles > spec.milesMax) return false;
    }

    // condition exclude: enrichment-only. Known-and-flagged => hide; unknown => keep (1C-A).
    if (spec.excludeConditions && spec.excludeConditions.length) {
      var cond = (car.details && car.details.condition) || null;
      if (cond && cond.some(function (c) { return spec.excludeConditions.indexOf(c) !== -1; })) {
        return false;
      }
    }
    return true;
  }

  function filterCars(cars, spec) {
    if (!spec) return cars.slice();
    return cars.filter(function (c) { return matchesFilter(c, spec); });
  }

  // how many of `cars` actually have the data a given enrichment-only filter needs — so the
  // UI can say "mileage known for N of M" instead of silently filtering on partial data.
  function knownCount(cars, field) {
    return cars.reduce(function (n, c) {
      var d = c.details;
      if (field === "miles") return n + (d && d.miles != null ? 1 : 0);
      if (field === "condition") return n + (d && d.condition ? 1 : 0);
      return n;
    }, 0);
  }

  var api = { matchesFilter: matchesFilter, filterCars: filterCars, knownCount: knownCount,
              DEAL_FILTER_MARGIN: DEAL_FILTER_MARGIN };
  if (typeof module !== "undefined" && module.exports) module.exports = api; // node tests
  global.BATFilters = api;                                                   // browser
})(typeof window !== "undefined" ? window : globalThis);
