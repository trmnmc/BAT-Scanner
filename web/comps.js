// web/comps.js — pure historical-comp logic, shared by the browser and Node tests.
//
// Comps are PAST sold listings (data/comps.json), shown as faded gray context on the Market-context
// map (price vs year). These functions validate raw comp rows and turn the good ones into plot
// points — a malformed/missing-price/missing-year row is SKIPPED, never patched with a fake value,
// and a missing/empty/garbage comp list yields []. Same IIFE + CommonJS pattern as the other
// web/*.js modules. No network, no scoring — Stage 3 only places real sold prices in space.
(function (global) {
  "use strict";

  function _num(v) { return (typeof v === "number" && isFinite(v)) ? v : null; }

  // A usable comp has a real positive price and a plausible 4-digit year. Anything else (missing,
  // wrong type, non-finite, out-of-range) is not usable — we drop it rather than invent a value.
  function isValidComp(row) {
    if (!row || typeof row !== "object") return false;
    var price = _num(row.price), year = _num(row.year);
    return price != null && price > 0 && year != null && year >= 1800 && year <= 2100;
  }

  // sold_ts is unix SECONDS in the snapshot; return ms, or null when absent/garbage (never faked).
  function compSoldMs(row) {
    var ts = row && row.sold_ts;
    if (typeof ts !== "number" || !isFinite(ts) || ts <= 0) return null;
    return ts < 1e12 ? ts * 1000 : ts;   // seconds -> ms (already-ms values pass through)
  }
  function compSoldDate(row) { var ms = compSoldMs(row); return ms ? new Date(ms) : null; }

  // A single comp -> { comp, x: price, y: year, soldMs } or null when the row isn't usable.
  function compPoint(row) {
    if (!isValidComp(row)) return null;
    return { comp: row, x: row.price, y: row.year, soldMs: compSoldMs(row) };
  }

  // All usable comps as plot points; bad rows are skipped, not fatal. A non-array (missing/empty/
  // malformed top-level) yields [] so the page keeps working when comps.json is absent or broken.
  function buildCompPoints(comps) {
    if (!Array.isArray(comps)) return [];
    var out = [];
    for (var i = 0; i < comps.length; i++) {
      var p = compPoint(comps[i]);
      if (p) out.push(p);
    }
    return out;
  }

  function _hay(comp) {
    return [comp.title, comp.make, comp.model].filter(Boolean).join(" ").toLowerCase();
  }

  // "Narrow historical dots where possible" (spec rule 9): apply the make/model/year/price/keyword
  // parts of a live-filter spec to a comp. Live-only constraints (no_reserve, dealsOnly, miles,
  // ending-soon) don't apply to a settled sale, so they're ignored — comps stay as context.
  function compMatchesSpec(comp, spec) {
    if (!spec) return true;
    if (spec.makes && spec.makes.length) {
      var mk = comp.make ? String(comp.make).toLowerCase() : null;
      if (!mk || spec.makes.map(function (s) { return String(s).toLowerCase(); }).indexOf(mk) === -1) return false;
    }
    if (spec.yearMin != null && (comp.year == null || comp.year < spec.yearMin)) return false;
    if (spec.yearMax != null && (comp.year == null || comp.year > spec.yearMax)) return false;
    if (spec.priceMin != null && (comp.price == null || comp.price < spec.priceMin)) return false;
    if (spec.priceMax != null && (comp.price == null || comp.price > spec.priceMax)) return false;
    var hay = _hay(comp);
    if (spec.models && spec.models.length
        && !spec.models.some(function (m) { return hay.indexOf(String(m).toLowerCase()) !== -1; })) return false;
    if (spec.requiredTerms && spec.requiredTerms.length
        && !spec.requiredTerms.every(function (t) { return hay.indexOf(String(t).toLowerCase()) !== -1; })) return false;
    if (spec.excludedTerms && spec.excludedTerms.length
        && spec.excludedTerms.some(function (t) { return hay.indexOf(String(t).toLowerCase()) !== -1; })) return false;
    if (spec.category && (!comp.category_ids || comp.category_ids.indexOf(spec.category) === -1)) return false;
    return true;
  }

  // Usable comp points narrowed by an optional spec.
  function filterComps(comps, spec) {
    return buildCompPoints(comps).filter(function (p) { return compMatchesSpec(p.comp, spec); });
  }

  var api = {
    isValidComp: isValidComp,
    compSoldMs: compSoldMs,
    compSoldDate: compSoldDate,
    compPoint: compPoint,
    buildCompPoints: buildCompPoints,
    compMatchesSpec: compMatchesSpec,
    filterComps: filterComps,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;  // node tests
  global.BATComps = api;                                                      // browser
})(typeof window !== "undefined" ? window : globalThis);
