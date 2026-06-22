// web/search.js — search spec sanitizer + interpreter adapter (browser + Node).
//
// The final match always happens locally through BATFilters.filterCars. This module
// only turns a query into a *validated* filter spec. Two paths:
//   - no endpoint  -> basicKeywordSpec(): honest keyword AND-search over the text fields.
//   - endpoint set -> interpretSearch(): POST the query to a server-side LLM that returns
//                     a filter spec, which is normalized + validated before it is applied.
//
// There is NO LLM provider here and NO secret. The endpoint URL is a public, non-secret
// config value (window.BAT_SEARCH_ENDPOINT or a <meta> tag). If the endpoint fails, times
// out, or returns an invalid spec, we fall back to basic keyword search — the map never breaks.
(function (global) {
  "use strict";

  // Only these fields are ever accepted from an interpreter. Anything else is dropped.
  var STRING_ARRAY_FIELDS = ["makes", "models", "requiredTerms", "excludedTerms"];
  var NUMBER_FIELDS = ["yearMin", "yearMax", "priceMin", "priceMax", "milesMax", "endingWithinHours"];
  var BOOL_FIELDS = ["noReserve", "dealsOnly"];

  var MAX_ARRAY = 40;          // cap array sizes
  var MAX_STR = 48;            // cap each string length
  var MAX_TERM_GROUPS = 12;    // cap number of OR groups
  var MAX_GROUP_TERMS = 24;    // cap terms per group
  var MAX_SERIALIZED = 8000;   // cap total serialized spec size (chars)
  var YEAR_LO = 1885, YEAR_HI = 2100;
  var MAX_HOURS = 24 * 366;    // ~1 year cap for endingWithinHours

  function isPlainObject(o) { return !!o && typeof o === "object" && !Array.isArray(o); }
  function finiteNum(v) { var n = Number(v); return Number.isFinite(n) ? n : null; }

  function _normStr(s, upper) {
    var t = String(s == null ? "" : s).trim();
    if (t.length > MAX_STR) t = t.slice(0, MAX_STR);
    return upper ? t.toUpperCase() : t.toLowerCase();
  }

  function _cleanStrArray(arr, upper) {
    if (!Array.isArray(arr)) return undefined;
    var out = [], seen = {};
    for (var i = 0; i < arr.length && out.length < MAX_ARRAY; i++) {
      var t = _normStr(arr[i], upper);
      if (!t || seen[t]) continue;
      seen[t] = 1;
      out.push(t);
    }
    return out.length ? out : undefined;
  }

  // Return a NEW sanitized spec. Never throws; never mutates the input. Drops unknown
  // fields, empties, and impossible values; clamps years/hours.
  function normalizeSearchSpec(raw) {
    var spec = {};
    if (!isPlainObject(raw)) return spec;

    STRING_ARRAY_FIELDS.forEach(function (f) {
      var v = _cleanStrArray(raw[f], false);
      if (v) spec[f] = v;
    });
    var cur = _cleanStrArray(raw.currencies, true);
    if (cur) spec.currencies = cur;

    if (Array.isArray(raw.termGroups)) {
      var groups = [];
      for (var i = 0; i < raw.termGroups.length && groups.length < MAX_TERM_GROUPS; i++) {
        var inner = raw.termGroups[i];
        if (!Array.isArray(inner)) continue;
        var clean = [], seen = {};
        for (var j = 0; j < inner.length && clean.length < MAX_GROUP_TERMS; j++) {
          var t = _normStr(inner[j], false);
          if (!t || seen[t]) continue;
          seen[t] = 1; clean.push(t);
        }
        if (clean.length) groups.push(clean);
      }
      if (groups.length) spec.termGroups = groups;
    }

    NUMBER_FIELDS.forEach(function (f) {
      var n = finiteNum(raw[f]);
      if (n == null) return;
      if (f === "yearMin" || f === "yearMax") {
        n = Math.round(Math.min(YEAR_HI, Math.max(YEAR_LO, n)));
      } else if (f === "endingWithinHours") {
        if (n <= 0) return;
        n = Math.min(MAX_HOURS, n);
      } else {
        if (n < 0) return;        // negative price/miles is impossible -> drop
        n = Math.round(n);
      }
      spec[f] = n;
    });

    BOOL_FIELDS.forEach(function (f) { if (raw[f] === true) spec[f] = true; });

    // total-size guard: if somehow oversized, shed the heaviest text fields in order.
    var shed = ["excludedTerms", "termGroups", "requiredTerms", "models", "makes"];
    var si = 0;
    while (JSON.stringify(spec).length > MAX_SERIALIZED && si < shed.length) {
      delete spec[shed[si++]];
    }
    return spec;
  }

  // Structural validation of a raw spec. Returns { valid, errors }. Unknown fields are
  // allowed (they get dropped by the normalizer); malformed KNOWN fields are errors.
  function validateSearchSpec(raw) {
    var errors = [];
    if (!isPlainObject(raw)) return { valid: false, errors: ["spec is not an object"] };

    NUMBER_FIELDS.forEach(function (f) {
      if (raw[f] == null) return;
      var n = Number(raw[f]);
      if (!Number.isFinite(n)) { errors.push(f + " is not a finite number"); return; }
      if ((f === "priceMin" || f === "priceMax" || f === "milesMax") && n < 0) errors.push(f + " is negative");
      if ((f === "yearMin" || f === "yearMax") && (n < 1800 || n > YEAR_HI)) errors.push(f + " is out of range");
    });
    STRING_ARRAY_FIELDS.concat(["currencies"]).forEach(function (f) {
      if (raw[f] != null && !Array.isArray(raw[f])) errors.push(f + " must be an array");
    });
    if (raw.termGroups != null) {
      if (!Array.isArray(raw.termGroups)) errors.push("termGroups must be an array");
      else if (!raw.termGroups.every(function (g) { return Array.isArray(g); })) errors.push("termGroups must be arrays of arrays");
    }
    try {
      if (JSON.stringify(raw).length > MAX_SERIALIZED * 4) errors.push("spec is too large");
    } catch (e) { errors.push("spec is not serializable"); }

    return { valid: errors.length === 0, errors: errors };
  }

  function isEmptySpec(spec) {
    if (!isPlainObject(spec)) return true;
    return Object.keys(spec).length === 0;
  }

  // Delegate to the filter engine so there is exactly one searchable-text definition.
  function buildSearchableText(car) {
    var F = (typeof module !== "undefined" && module.exports)
      ? require("./filters.js") : global.BATFilters;
    if (F && F.buildSearchableText) return F.buildSearchableText(car);
    // fallback (kept in sync with filters.js)
    var parts = [car && car.title];
    if (car && car.make) { parts.push(car.make.name); parts.push(car.make.slug); }
    (car && car.models || []).forEach(function (m) { if (m) { parts.push(m.slug); parts.push(m.name); } });
    (car && car.taxonomy_paths || []).forEach(function (p) { parts.push(p); });
    return String(parts.filter(Boolean).join(" ")).toLowerCase().replace(/[^a-z0-9]+/g, " ").replace(/\s+/g, " ").trim();
  }

  // Honest basic keyword search: every word must appear in the text (AND).
  function basicKeywordSpec(query) {
    var norm = String(query == null ? "" : query).toLowerCase().replace(/[^a-z0-9]+/g, " ").replace(/\s+/g, " ").trim();
    if (!norm) return {};
    var seen = {}, terms = [];
    norm.split(" ").forEach(function (w) {
      if (w && !seen[w] && terms.length < MAX_ARRAY) { seen[w] = 1; terms.push(w); }
    });
    return terms.length ? { requiredTerms: terms } : {};
  }

  function _fmtMoney(n) { return "$" + Number(n).toLocaleString(); }

  // One-line human summary of the active constraints in a (normalized) spec.
  function formatSearchSummary(spec) {
    if (isEmptySpec(spec)) return "Showing the whole board";
    var bits = [];
    if (spec.makes) bits.push(spec.makes.join(", "));
    if (spec.models) bits.push(spec.models.join(", "));
    if (spec.yearMin || spec.yearMax) bits.push([spec.yearMin || "", spec.yearMax || ""].join("–").replace(/^–|–$/, function (m) { return m === "–" ? "" : m; }) || "");
    if (spec.priceMin != null) bits.push("≥ " + _fmtMoney(spec.priceMin));
    if (spec.priceMax != null) bits.push("≤ " + _fmtMoney(spec.priceMax));
    if (spec.currencies) bits.push(spec.currencies.join("/"));
    if (spec.noReserve) bits.push("no reserve");
    if (spec.dealsOnly) bits.push("deals only");
    if (spec.milesMax != null) bits.push("≤ " + Number(spec.milesMax).toLocaleString() + " mi");
    if (spec.endingWithinHours != null) bits.push("ending ≤ " + spec.endingWithinHours + "h");
    if (spec.requiredTerms) bits.push(spec.requiredTerms.join(" + "));
    if (spec.termGroups) spec.termGroups.forEach(function (g) { bits.push(g.join("/")); });
    if (spec.excludedTerms) bits.push("not: " + spec.excludedTerms.join(", "));
    return bits.filter(Boolean).join(" · ");
  }

  // [{ key, field, label }] — one per active constraint, for removable chips. Array-valued
  // term groups expose a per-group key so a single group can be removed.
  function specConstraints(spec) {
    if (isEmptySpec(spec)) return [];
    var out = [];
    function push(field, label) { out.push({ key: field, field: field, label: label }); }
    if (spec.makes) push("makes", "make: " + spec.makes.join(", "));
    if (spec.models) push("models", "model: " + spec.models.join(", "));
    if (spec.yearMin != null) push("yearMin", "year ≥ " + spec.yearMin);
    if (spec.yearMax != null) push("yearMax", "year ≤ " + spec.yearMax);
    if (spec.priceMin != null) push("priceMin", "≥ " + _fmtMoney(spec.priceMin));
    if (spec.priceMax != null) push("priceMax", "≤ " + _fmtMoney(spec.priceMax));
    if (spec.currencies) push("currencies", spec.currencies.join("/"));
    if (spec.noReserve) push("noReserve", "no reserve");
    if (spec.dealsOnly) push("dealsOnly", "deals only");
    if (spec.milesMax != null) push("milesMax", "≤ " + Number(spec.milesMax).toLocaleString() + " mi");
    if (spec.endingWithinHours != null) push("endingWithinHours", "ending ≤ " + spec.endingWithinHours + "h");
    if (spec.requiredTerms) push("requiredTerms", spec.requiredTerms.join(" + "));
    if (spec.termGroups) spec.termGroups.forEach(function (g, i) {
      out.push({ key: "termGroups:" + i, field: "termGroups", index: i, label: g.join("/") });
    });
    if (spec.excludedTerms) push("excludedTerms", "not: " + spec.excludedTerms.join(", "));
    return out;
  }

  // Build the small, non-sensitive catalog sent to the interpreter.
  function buildCatalog(cars) {
    var makesBy = {}, currencies = {};
    (cars || []).forEach(function (c) {
      var m = c.make;
      if (m && m.slug && !makesBy[m.slug]) makesBy[m.slug] = { name: m.name || m.slug, slug: m.slug };
      var cur = c.bid && c.bid.currency;
      if (cur) currencies[cur] = 1;
    });
    var makes = Object.keys(makesBy).sort().map(function (s) { return makesBy[s]; });
    return { makes: makes, currencies: Object.keys(currencies).sort() };
  }

  // Turn a query into a usable spec. Returns a Promise resolving to:
  //   { spec, source: "keyword"|"llm", summary, error? }
  // options: { endpoint, fetchImpl, catalog, timeoutMs }. fetchImpl is injectable for tests.
  function interpretSearch(query, options) {
    options = options || {};
    var endpoint = options.endpoint;
    var keyword = function (error) {
      var spec = basicKeywordSpec(query);
      return { spec: spec, source: "keyword", summary: formatSearchSummary(spec), error: error || null };
    };
    if (!endpoint) return Promise.resolve(keyword());

    var fetchImpl = options.fetchImpl || (typeof fetch !== "undefined" ? fetch : null);
    if (!fetchImpl) return Promise.resolve(keyword("no fetch available"));

    var timeoutMs = options.timeoutMs || 8000;
    var controller = (typeof AbortController !== "undefined") ? new AbortController() : null;
    var timer = setTimeout(function () { if (controller) controller.abort(); }, timeoutMs);
    var body = JSON.stringify({ version: 1, query: String(query == null ? "" : query), catalog: options.catalog || {} });

    return Promise.resolve()
      .then(function () {
        return fetchImpl(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: body,
          signal: controller ? controller.signal : undefined,
        });
      })
      .then(function (res) {
        if (!res || !res.ok) throw new Error("endpoint HTTP error");
        return res.json();
      })
      .then(function (data) {
        var rawSpec = data && data.spec;
        var check = validateSearchSpec(rawSpec);
        var normalized = normalizeSearchSpec(rawSpec);
        if (!check.valid || isEmptySpec(normalized)) return keyword("interpreter returned an unusable spec");
        var summary = (data && typeof data.summary === "string" && data.summary.trim())
          ? data.summary.trim() : formatSearchSummary(normalized);
        return { spec: normalized, source: "llm", summary: summary, error: null };
      })
      .catch(function (e) {
        return keyword((e && e.name === "AbortError") ? "interpreter timed out" : "interpreter unavailable");
      })
      .then(function (result) { clearTimeout(timer); return result; });
  }

  var api = {
    normalizeSearchSpec: normalizeSearchSpec,
    validateSearchSpec: validateSearchSpec,
    isEmptySpec: isEmptySpec,
    buildSearchableText: buildSearchableText,
    basicKeywordSpec: basicKeywordSpec,
    formatSearchSummary: formatSearchSummary,
    specConstraints: specConstraints,
    buildCatalog: buildCatalog,
    interpretSearch: interpretSearch,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api; // node tests
  global.BATSearch = api;                                                     // browser
})(typeof window !== "undefined" ? window : globalThis);
