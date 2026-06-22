// web/filters.js — ONE filter spec, ONE predicate (pivot 2026-06-21).
// The map, the search box, saved views, and (later) a server-side LLM interpreter all
// produce the SAME spec and run through matchesFilter, so the LLM is a natural-language
// mouth on this engine, not a second search system. The LLM never matches cars itself;
// it only returns a spec that is validated and then run here.
//
// spec shape (every field optional; omitted = no constraint):
//   { makes: ["porsche", ...],          // car.make.slug in this list (OR)
//     models: ["911", "e30"],           // term found in model slug/name, title, or taxonomy (OR)
//     yearMin, yearMax,                 // inclusive era
//     priceMin, priceMax,               // current bid, in the car's own currency (not converted)
//     currencies: ["USD","EUR"],        // bid.currency in this list (OR)
//     noReserve: true,                  // only no-reserve auctions
//     dealsOnly: true,                  // only cars priced under comps (scoreable + deal_pct >= margin)
//     milesMax: 60000,                  // enrichment-only; UNKNOWN mileage PASSES (1C-A)
//     excludeConditions: ["restomod"],  // enrichment-only; unknown condition PASSES (1C-A)
//     endingWithinHours: 48,            // valid future ends_at within this many hours
//     requiredTerms: ["diesel"],        // EVERY term must appear in the searchable text
//     termGroups: [["wagon","avant"]],  // each inner array is an OR group; EVERY group must hit
//     excludedTerms: ["project"],       // ANY hit rejects the car
//     category: "vintage-trucks" }      // legacy/optional: car tagged with this category_id
//
// Honesty rule (1C-A): filters that need per-listing enrichment (mileage, condition) never
// hide a car whose data we haven't fetched — unknown passes through, so you never lose a
// car to missing data, only to data that actually fails the filter.
(function (global) {
  "use strict";

  var DEAL_FILTER_MARGIN = 0.15; // "deals only" = at least this far under comps
  var SCOREABLE = { "make-model-y3": 1, "make-model-y7": 1 };
  var HOUR_MS = 3600 * 1000;

  function isScoreableDeal(v) {
    return !!(v && v.deal_pct != null && SCOREABLE[v.basis] && v.deal_pct >= DEAL_FILTER_MARGIN);
  }

  // lowercase, punctuation -> spaces, collapse whitespace. Used for term matching so
  // "5-speed" and "5 Speed" compare equal.
  function normalizeText(s) {
    return String(s == null ? "" : s).toLowerCase().replace(/[^a-z0-9]+/g, " ").replace(/\s+/g, " ").trim();
  }

  function _modelStrings(car) {
    var parts = [];
    (car.models || []).forEach(function (m) {
      if (m && m.slug) parts.push(m.slug);
      if (m && m.name) parts.push(m.name);
    });
    (car.taxonomy_paths || []).forEach(function (p) { if (p) parts.push(p); });
    return parts;
  }

  // The text searched by models/requiredTerms/termGroups/excludedTerms: title + make
  // name & slug + model names & slugs + taxonomy paths, all normalized.
  function buildSearchableText(car) {
    if (!car) return "";
    var parts = [car.title];
    if (car.make) { parts.push(car.make.name); parts.push(car.make.slug); }
    parts = parts.concat(_modelStrings(car));
    return normalizeText(parts.filter(Boolean).join(" "));
  }

  function _term(haystack, term) {
    var t = normalizeText(term);
    return t.length > 0 && haystack.indexOf(t) !== -1;
  }

  function matchesFilter(car, spec, nowMs) {
    if (!spec) return true;
    var bid = car.bid && car.bid.amount;

    if (spec.priceMin != null && (bid == null || bid < spec.priceMin)) return false;
    if (spec.priceMax != null && (bid == null || bid > spec.priceMax)) return false;

    var y = car.year;
    if (spec.yearMin != null && (y == null || y < spec.yearMin)) return false;
    if (spec.yearMax != null && (y == null || y > spec.yearMax)) return false;

    if (spec.makes && spec.makes.length) {
      var mk = car.make && car.make.slug;
      mk = mk ? String(mk).toLowerCase() : null;
      if (!mk || spec.makes.map(function (s) { return String(s).toLowerCase(); }).indexOf(mk) === -1) return false;
    }

    if (spec.currencies && spec.currencies.length) {
      var cur = car.bid && car.bid.currency;
      cur = cur ? String(cur).toUpperCase() : null;
      if (!cur || spec.currencies.map(function (s) { return String(s).toUpperCase(); }).indexOf(cur) === -1) return false;
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

    // ending soon: needs a valid future ends_at within the window.
    if (spec.endingWithinHours != null) {
      var end = Date.parse(car.ends_at);
      if (isNaN(end)) return false;
      var now = typeof nowMs === "number" ? nowMs : Date.now();
      var left = end - now;
      if (left <= 0 || left > spec.endingWithinHours * HOUR_MS) return false;
    }

    // models: OR over the requested model terms, searched in model fields + title + taxonomy.
    if (spec.models && spec.models.length) {
      var modelHay = normalizeText([car.title].concat(_modelStrings(car)).filter(Boolean).join(" "));
      var modelHit = spec.models.some(function (m) { return _term(modelHay, m); });
      if (!modelHit) return false;
    }

    // free-text term logic operates on the searchable text.
    var hay = null;
    function HAY() { if (hay == null) hay = buildSearchableText(car); return hay; }

    if (spec.requiredTerms && spec.requiredTerms.length) {
      var allPresent = spec.requiredTerms.every(function (t) { return _term(HAY(), t); });
      if (!allPresent) return false;
    }

    if (spec.termGroups && spec.termGroups.length) {
      var everyGroupHits = spec.termGroups.every(function (group) {
        if (!group || !group.length) return true;                 // empty group = no constraint
        return group.some(function (t) { return _term(HAY(), t); });
      });
      if (!everyGroupHits) return false;
    }

    if (spec.excludedTerms && spec.excludedTerms.length) {
      var anyExcluded = spec.excludedTerms.some(function (t) { return _term(HAY(), t); });
      if (anyExcluded) return false;
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

  function filterCars(cars, spec, nowMs) {
    if (!spec) return cars.slice();
    return cars.filter(function (c) { return matchesFilter(c, spec, nowMs); });
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
              buildSearchableText: buildSearchableText, normalizeText: normalizeText,
              DEAL_FILTER_MARGIN: DEAL_FILTER_MARGIN };
  if (typeof module !== "undefined" && module.exports) module.exports = api; // node tests
  global.BATFilters = api;                                                   // browser
})(typeof window !== "undefined" ? window : globalThis);
