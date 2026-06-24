// web/watchlists.js — manual + suggested watchlists, deterministic phrase parsing, and watchlist
// matching. Split out of the former web/storage.js (Stage 5 refactor).
//
// Builds on web/user-state.js's store-core (ONE persistence layer, not two) and REUSES web/filters.js
// (matchesFilter) + web/search.js (specConstraints + spec sanitizer) — there is no second filtering
// or search engine here. A watchlist is just a saved, validated filter spec (+ an optional non-spec
// predicate for fields a spec can't express). Matching always runs through BATFilters. No network.
(function (global) {
  "use strict";

  var _node = (typeof module !== "undefined" && module.exports);
  var _US = _node ? require("./user-state.js") : global.BATUserState;  // store-core + profiles (shared)
  var _F = _node ? require("./filters.js") : global.BATFilters;        // matching engine (reused)
  var _S = _node ? require("./search.js") : global.BATSearch;          // spec sanitizer + constraints (reused)

  var WATCHLIST_SCHEMA = 1;
  var SOURCES = ["manual", "suggested", "ai"];
  var DEFAULT_PROFILE = "me";
  var KEYS = {
    watchlists: "bat_watchlists_v1",
    legacySaved: "bat_saved_views_v1",   // pre-Stage-5 saved views; migrated, NEVER erased
  };

  // Suggested watchlist TEMPLATES — deterministic spec + optional per-car predicate. The spec part is
  // matched by BATFilters (reused); the predicate is a tiny deterministic check for fields a filter
  // spec can't express (engagement/value). These are configurable data, not a matching engine.
  var SUGGESTED = [
    { key: "future-collectibles", name: "Future collectibles", note: "Modern, low-mileage", spec: { yearMin: 2008, milesMax: 40000 } },
    { key: "quiet-auctions", name: "Quiet auctions", note: "Few comments so far", spec: {},
      predicate: function (c) { var x = c && c.engagement && c.engagement.comments; return typeof x === "number" && x <= 5; } },
    { key: "investment-grade", name: "Investment-grade cars", note: "High-value, no reserve", spec: { priceMin: 75000, noReserve: true } },
    { key: "weird-specs", name: "Weird specs", note: "Modified / unusual builds", spec: {},
      predicate: function (c) { var cond = (c && c.details && c.details.condition) || [];
        return ["modified", "engine-swap", "restomod", "kit-car", "replica", "tribute"].some(function (f) { return cond.indexOf(f) !== -1; }); } },
    { key: "enthusiast-gems", name: "Enthusiast gems", note: "Enthusiast brands", spec: { makes: ["porsche", "bmw", "honda", "toyota", "mazda", "lotus", "acura", "subaru", "nissan"] } },
    { key: "below-market", name: "Below-market candidates", note: "Under comps, no reserve", spec: { dealsOnly: true } },
    { key: "high-upside-driver", name: "High-upside driver cars", note: "Drivable + some upside", spec: { milesMax: 120000 },
      // deal_pct is only "upside" on a no-reserve, trusted-comp basis — a reserve bid is not a
      // bargain and an insufficient-basis deal_pct is not trusted (mirrors map-data/filters gating).
      predicate: function (c) {
        var v = c && c.value;
        return !!(v && c.flags && c.flags.no_reserve
          && (v.basis === "make-model-y3" || v.basis === "make-model-y7")
          && typeof v.deal_pct === "number" && v.deal_pct > 0);
      } },
  ];

  function _isObj(v) { return v != null && typeof v === "object" && !Array.isArray(v); }
  function _isProfile(p) { return _US && _US.PROFILES.indexOf(p) !== -1; }
  function _isSource(s) { return SOURCES.indexOf(s) !== -1; }

  // Only known BATFilters/BATSearch spec keys are persisted, so a large auction record accidentally
  // passed as a "spec" can't bloat localStorage (enforces the "no large records" contract).
  var SPEC_KEYS = ["makes", "models", "yearMin", "yearMax", "priceMin", "priceMax", "milesMax",
    "currencies", "category", "noReserve", "dealsOnly", "requiredTerms", "termGroups",
    "excludedTerms", "excludeConditions", "endingWithinHours"];
  function _cleanSpec(spec) {
    if (!_isObj(spec)) return {};
    var out = {};
    SPEC_KEYS.forEach(function (k) { if (spec[k] != null) out[k] = spec[k]; });
    return out;
  }

  // ====================================================================================
  // Deterministic, NO-AI phrase -> spec. Recognizes makes + origin groups, price ceilings/
  // floors, no-reserve, an "ending within N" window, era cues (air-cooled, decades, pre/after
  // YEAR), and common engine terms; any leftover meaningful words fall back to a keyword AND.
  // The result is sanitized through BATSearch.normalizeSearchSpec, so it is the SAME validated
  // spec shape the search box and saved views produce — matching still runs through BATFilters.
  // ====================================================================================

  // common make words/abbreviations -> canonical BaT make slug.
  var MAKE_ALIASES = {
    porsche: "porsche", bmw: "bmw", mercedes: "mercedes-benz", "mercedes-benz": "mercedes-benz", benz: "mercedes-benz",
    audi: "audi", vw: "volkswagen", volkswagen: "volkswagen", ferrari: "ferrari", lamborghini: "lamborghini", lambo: "lamborghini",
    ford: "ford", chevrolet: "chevrolet", chevy: "chevrolet", toyota: "toyota", honda: "honda", nissan: "nissan",
    datsun: "datsun", mazda: "mazda", subaru: "subaru", mitsubishi: "mitsubishi", lexus: "lexus", acura: "acura",
    infiniti: "infiniti", jaguar: "jaguar", jag: "jaguar", "land-rover": "land-rover", landrover: "land-rover", "range-rover": "land-rover",
    aston: "aston-martin", "aston-martin": "aston-martin", lotus: "lotus", alfa: "alfa-romeo", "alfa-romeo": "alfa-romeo",
    fiat: "fiat", lancia: "lancia", mini: "mini", jeep: "jeep", dodge: "dodge", chrysler: "chrysler", plymouth: "plymouth",
    cadillac: "cadillac", buick: "buick", pontiac: "pontiac", oldsmobile: "oldsmobile", volvo: "volvo", saab: "saab",
    maserati: "maserati", bentley: "bentley", "rolls-royce": "rolls-royce", mclaren: "mclaren", tesla: "tesla",
    lincoln: "lincoln", gmc: "gmc", triumph: "triumph", mg: "mg", austin: "austin",
  };
  // nationality / region words -> a set of make slugs.
  var ORIGIN_MAKES = {
    japanese: ["toyota", "honda", "nissan", "datsun", "mazda", "subaru", "mitsubishi", "lexus", "acura", "infiniti"],
    jdm: ["toyota", "honda", "nissan", "datsun", "mazda", "subaru", "mitsubishi", "lexus", "acura", "infiniti"],
    german: ["porsche", "bmw", "mercedes-benz", "audi", "volkswagen"],
    italian: ["ferrari", "lamborghini", "alfa-romeo", "fiat", "maserati", "lancia"],
    british: ["jaguar", "land-rover", "aston-martin", "lotus", "mini", "bentley", "rolls-royce", "mclaren", "triumph", "mg", "austin"],
    american: ["ford", "chevrolet", "dodge", "chrysler", "cadillac", "buick", "pontiac", "jeep", "lincoln", "gmc", "plymouth", "oldsmobile"],
  };
  // a few iconic bare model codes that should become model terms, not free keywords.
  var MODEL_TOKENS = ["911", "912", "914", "930", "356", "928", "944", "718", "959", "964", "993",
    "e30", "e36", "e46", "e9", "2002", "m3", "240z", "260z", "280z", "300zx", "rx7", "rx-7", "mr2",
    "miata", "supra", "gtr", "gt-r", "nsx", "s2000", "f40", "f50", "countach", "testarossa", "bronco", "defender"];
  // engine terms -> an OR group of normalized variants (normalizeText collapses "V-8" -> "v 8").
  var ENGINE_TERMS = [
    { re: /\bv-?12\b/, group: ["v12", "v 12"] },
    { re: /\bv-?10\b/, group: ["v10", "v 10"] },
    { re: /\bv-?8\b/, group: ["v8", "v 8"] },
    { re: /\bv-?6\b/, group: ["v6", "v 6"] },
    { re: /\b(flat[\s-]?6|flat[\s-]?six)\b/, group: ["flat 6", "flat six"] },
    { re: /\b(inline[\s-]?6|straight[\s-]?6|i6|inline[\s-]?six|straight[\s-]?six)\b/, group: ["inline 6", "straight 6"] },
    { re: /\b(turbo|turbocharged)\b/, group: ["turbo"] },
  ];
  // dropped before the keyword fallback: chit-chat words AND the structural words consumed by the
  // price / time / era / no-reserve patterns above (so "under", "ending", "air-cooled" etc. never
  // leak through as keyword terms).
  var STOPWORDS = { car: 1, cars: 1, sport: 1, sports: 1, the: 1, a: 1, an: 1, with: 1, and: 1, or: 1,
    for: 1, in: 1, of: 1, classic: 1, vintage: 1, cool: 1, nice: 1, clean: 1, good: 1, great: 1,
    "$": 1, me: 1, some: 1, any: 1, looking: 1, want: 1,
    // price qualifiers
    under: 1, below: 1, over: 1, above: 1, less: 1, than: 1, more: 1, up: 1, to: 1, max: 1, maximum: 1,
    min: 1, minimum: 1, at: 1, least: 1, starting: 1, cheaper: 1, grand: 1, around: 1, about: 1, k: 1,
    // time-window qualifiers
    no: 1, reserve: 1, ending: 1, ends: 1, end: 1, within: 1, hour: 1, hours: 1, hr: 1, hrs: 1, day: 1, days: 1,
    // era qualifiers
    "air-cooled": 1, aircooled: 1, air: 1, cooled: 1, pre: 1, post: 1, before: 1, after: 1, newer: 1, older: 1,
    // engine descriptors (the engine OR-group already captures the intent; keep the bare words out of requiredTerms)
    flat: 1, six: 1, inline: 1, straight: 1, i6: 1 };

  // multi-word make names ("land rover", "rolls royce") the per-token pass can't see — built from the
  // hyphenated MAKE_ALIASES keys plus a couple of common spellings. Resolved BEFORE per-token matching.
  var MULTIWORD_MAKES = { "range rover": "land-rover" };
  Object.keys(MAKE_ALIASES).forEach(function (k) { if (k.indexOf("-") !== -1) MULTIWORD_MAKES[k.replace(/-/g, " ")] = MAKE_ALIASES[k]; });

  function _moneyToNumber(numStr, kFlag) {
    var n = parseFloat(String(numStr).replace(/[, ]/g, ""));
    if (!isFinite(n)) return null;
    return Math.round(kFlag ? n * 1000 : n);
  }

  function parsePhrase(text) {
    var norm = String(text == null ? "" : text).toLowerCase();
    var spec = {};
    var consumed = {};   // lowercased word -> 1, removed before the keyword fallback
    function consume() { for (var i = 0; i < arguments.length; i++) consumed[arguments[i]] = 1; }

    // --- makes: multi-word names first, then explicit aliases + nationality groups ---
    var makesSet = {}, words = norm.split(/[^a-z0-9-]+/).filter(Boolean);
    Object.keys(MULTIWORD_MAKES).forEach(function (phrase) {
      if (norm.indexOf(phrase) !== -1) { makesSet[MULTIWORD_MAKES[phrase]] = 1; phrase.split(" ").forEach(function (w) { consume(w); }); }
    });
    words.forEach(function (w) {
      if (MAKE_ALIASES.hasOwnProperty(w)) { makesSet[MAKE_ALIASES[w]] = 1; consume(w); }
      if (ORIGIN_MAKES.hasOwnProperty(w)) { ORIGIN_MAKES[w].forEach(function (s) { makesSet[s] = 1; }); consume(w); }
    });

    // --- price: "under/below/up to $100k" (ceiling) and "over/above/at least $50k" (floor) ---
    var under = norm.match(/(?:under|below|less\s+than|up\s+to|max(?:imum)?|cheaper\s+than|<)\s*\$?\s*([\d.,]+)\s*(k|grand)?/);
    if (under) { var pm = _moneyToNumber(under[1], !!under[2]); if (pm != null) spec.priceMax = pm; }
    var over = norm.match(/(?:over|above|more\s+than|at\s+least|min(?:imum)?|starting\s+at|>)\s*\$?\s*([\d.,]+)\s*(k|grand)?/);
    if (over) { var pmin = _moneyToNumber(over[1], !!over[2]); if (pmin != null) spec.priceMin = pmin; }

    // --- no reserve ---
    if (/\bno[\s-]?reserve\b/.test(norm)) spec.noReserve = true;

    // --- ending within N hours / days ---
    var ending = norm.match(/ending\s+(?:within|in|under)\s+(\d+)\s*(hour|hours|hr|hrs|day|days)/);
    if (ending) {
      var n = parseInt(ending[1], 10);
      if (isFinite(n) && n > 0) spec.endingWithinHours = /day/.test(ending[2]) ? n * 24 : n;
    }

    // --- era cues ---
    // air-cooled: Porsche's air-cooled era ended with the 993 (model year 1998). Cap the year and,
    // when no make was named, assume Porsche (air-cooled is iconically Porsche on BaT).
    if (/\bair[\s-]?cooled\b/.test(norm)) {
      spec.yearMax = Math.min(spec.yearMax != null ? spec.yearMax : 9999, 1998);
      if (!Object.keys(makesSet).length) makesSet.porsche = 1;
    }
    // "pre-1990" / "before 1990" -> yearMax; "after 2010" / "post 2010" / "newer than 2010" -> yearMin.
    var preY = norm.match(/(?:pre|before)[\s-]?(\d{4})/);
    if (preY) spec.yearMax = Math.min(spec.yearMax != null ? spec.yearMax : 9999, parseInt(preY[1], 10) - 1);
    var postY = norm.match(/(?:after|post|newer\s+than)[\s-]?(\d{4})/);
    if (postY) spec.yearMin = Math.max(spec.yearMin != null ? spec.yearMin : 0, parseInt(postY[1], 10) + 1);
    // full decade, e.g. "1960s" or "1990s".
    var decade = norm.match(/\b((?:19|20)\d)0s\b/);
    if (decade) { var d0 = parseInt(decade[1], 10) * 10; spec.yearMin = d0; spec.yearMax = d0 + 9; }

    // --- engine terms -> termGroups ---
    var groups = [];
    ENGINE_TERMS.forEach(function (e) {
      if (e.re.test(norm)) { groups.push(e.group.slice()); e.group.forEach(function (g) { consume(g.replace(/\s+/g, "")); }); }
    });
    // also mark the raw engine token (e.g. "v8") consumed so it isn't echoed as a keyword.
    ["v12", "v10", "v8", "v6", "i6", "turbo", "turbocharged"].forEach(function (t) { if (norm.indexOf(t) !== -1) consume(t); });
    if (groups.length) spec.termGroups = groups;

    // --- iconic model tokens -> models ---
    var modelsSet = {};
    words.forEach(function (w) { if (MODEL_TOKENS.indexOf(w) !== -1) { modelsSet[w] = 1; consume(w); } });

    if (Object.keys(makesSet).length) spec.makes = Object.keys(makesSet);
    if (Object.keys(modelsSet).length) spec.models = Object.keys(modelsSet);

    // --- leftover meaningful words -> keyword AND (honest fallback, same as BATSearch). A token that
    // was wholly consumed/stopworded is skipped; otherwise it is split on hyphens so a structural token
    // the splitter kept intact ("no-reserve", "v-8", "flat-six") decomposes into already-handled pieces
    // and never leaks a spurious required term. ---
    var leftover = [];
    words.forEach(function (w) {
      if (consumed[w] || STOPWORDS[w]) return;       // whole token already handled by a pattern above
      w.split("-").forEach(function (piece) {
        if (piece && !consumed[piece] && !STOPWORDS[piece] && piece.length > 1
            && !/^[\d.,]+k?$/.test(piece)   // bare numbers / "$100k" amounts
            && !/\d{4}/.test(piece)) leftover.push(piece);   // year-bearing tokens
      });
    });
    if (leftover.length) {
      var have = {};
      (spec.requiredTerms || []).forEach(function (t) { have[t] = 1; });
      var extra = [];
      leftover.forEach(function (w) { if (!have[w]) { have[w] = 1; extra.push(w); } });
      if (extra.length) spec.requiredTerms = (spec.requiredTerms || []).concat(extra);
    }

    // sanitize through the SAME validator the search box uses (clamps years/prices, caps arrays,
    // drops empties/unknowns). Falls back to the local whitelist if search.js is somehow absent.
    return (_S && _S.normalizeSearchSpec) ? _S.normalizeSearchSpec(spec) : _cleanSpec(spec);
  }

  // ====================================================================================
  // Watchlist store (manual + suggested), matching, and legacy migration.
  // ====================================================================================

  function createWatchlists(backend, opts) {
    opts = opts || {};
    backend = backend || (_US ? _US.memoryBackend() : null);
    var now = typeof opts.now === "function" ? opts.now : function () { return Date.now(); };
    var seq = 0;
    var genId = typeof opts.genId === "function" ? opts.genId
      : function () { seq += 1; return "wl_" + now().toString(36) + "_" + seq.toString(36); };
    // active-profile source: the user-state instance if supplied, else a constant default. Callers
    // (index.html) always pass an explicit profile to list/enabled, so this default rarely matters.
    var getActiveProfile = (opts.userState && typeof opts.userState.getActiveProfile === "function")
      ? opts.userState.getActiveProfile : function () { return DEFAULT_PROFILE; };

    function _meta() { return _US.readMeta(backend); }
    function _setMeta(patch) { return _US.writeMeta(backend, patch); }

    // ---- raw watchlist store (manual + materialized), with read-time legacy-shape coercion ----
    // Old storage.js persisted watchlists with `profile_id`/`origin`; coerce those to `profile`/
    // `source` on read so any saved watchlist keeps loading after the refactor.
    function _coerceWatchlist(w) {
      if (!_isObj(w)) return null;
      var out = Object.assign({}, w);
      if (out.profile == null && out.profile_id != null) out.profile = out.profile_id;
      if (out.source == null && out.origin != null) out.source = out.origin;
      delete out.profile_id; delete out.origin;
      if (!_isProfile(out.profile)) out.profile = DEFAULT_PROFILE;
      if (!_isSource(out.source)) out.source = "manual";
      out.spec = _cleanSpec(out.spec);
      return out;
    }
    function _rawWatchlists() {
      var r = _US.read(backend, KEYS.watchlists, []);
      return Array.isArray(r.value) ? r.value.map(_coerceWatchlist).filter(Boolean) : [];
    }
    function _writeWatchlists(list) { return _US.write(backend, KEYS.watchlists, list); }

    // accepts both new (profile/source) and legacy (profile_id/origin) input keys.
    function _newWatchlist(o) {
      o = o || {};
      var t = now();
      var profile = o.profile != null ? o.profile : o.profile_id;
      var source = o.source != null ? o.source : o.origin;
      return {
        id: o.id || genId(),
        schema_version: WATCHLIST_SCHEMA,
        name: String(o.name || "Untitled"),
        original_query: typeof o.original_query === "string" ? o.original_query.slice(0, 200) : null,
        profile: _isProfile(profile) ? profile : DEFAULT_PROFILE,
        source: _isSource(source) ? source : "manual",
        enabled: o.enabled != null ? !!o.enabled : true,
        spec: _cleanSpec(o.spec),
        created_at: o.created_at || t,
        updated_at: t,
      };
    }

    // suggested watchlists are generated fresh from templates (the predicate can't serialize); only
    // their enabled-state is persisted, in meta.suggestedEnabled.
    function _suggestedWatchlists() {
      var enabled = _meta().suggestedEnabled || {};
      return SUGGESTED.map(function (tpl) {
        return {
          id: "suggested:" + tpl.key, schema_version: WATCHLIST_SCHEMA, name: tpl.name,
          original_query: null, profile: "shared", source: "suggested", enabled: !!enabled[tpl.key],
          spec: tpl.spec || {}, predicate: tpl.predicate || null, note: tpl.note || null,
          created_at: 0, updated_at: 0,
        };
      });
    }

    // ---- legacy migration: old saved views ({name,spec}) -> manual watchlists. Idempotent, and the
    //      legacy key is preserved (never erased), so an old build keeps working too. ----
    function migrateLegacy() {
      var meta = _meta();
      if (meta.migratedLegacy) return 0;
      var r = _US.read(backend, KEYS.legacySaved, []);
      var legacy = Array.isArray(r.value) ? r.value : [];
      var list = _rawWatchlists();
      var existingNames = {};
      list.forEach(function (w) { if (w.source === "manual") existingNames[w.name] = 1; });
      var added = 0;
      legacy.forEach(function (v) {
        if (!_isObj(v) || !v.name || existingNames[v.name]) return;
        list.push(_newWatchlist({ name: v.name, spec: _isObj(v.spec) ? v.spec : {}, profile: "shared", source: "manual", enabled: false }));
        existingNames[v.name] = 1;
        added += 1;
      });
      // Only mark migration done once the watchlist write actually succeeded — otherwise a quota /
      // private-mode failure would set the flag, skip retry next load, and orphan the legacy views.
      var wrote = added ? _writeWatchlists(list) : true;
      if (wrote) _setMeta({ migratedLegacy: true });
      return wrote ? added : 0;
    }

    // ---- watchlist CRUD ----
    function listWatchlists(profile, includeSuggested) {
      profile = _isProfile(profile) ? profile : getActiveProfile();
      var vis = _US.visibleProfiles(profile);
      var manual = _rawWatchlists().filter(function (w) { return vis.indexOf(w.profile) !== -1; });
      var sugg = includeSuggested === false ? [] : _suggestedWatchlists();
      return manual.concat(sugg);
    }
    function enabledWatchlists(profile) {
      return listWatchlists(profile, true).filter(function (w) { return w.enabled; });
    }
    function saveWatchlist(o) {
      var list = _rawWatchlists();
      var wl = _newWatchlist(o);
      list.unshift(wl);
      return _writeWatchlists(list) ? wl : null;   // null signals a failed persist (quota/private mode)
    }
    function updateWatchlist(id, patch) {
      if (typeof id === "string" && id.indexOf("suggested:") === 0) {       // suggested: only enabled toggles
        if (patch && "enabled" in patch) {
          var key = id.slice("suggested:".length);
          var en = Object.assign({}, _meta().suggestedEnabled || {});
          en[key] = !!patch.enabled;
          _setMeta({ suggestedEnabled: en });
        }
        return _suggestedWatchlists().filter(function (w) { return w.id === id; })[0] || null;
      }
      var list = _rawWatchlists(), found = null;
      list = list.map(function (w) {
        if (w.id !== id) return w;
        found = Object.assign({}, w, patch || {}, { updated_at: now(), id: w.id, schema_version: WATCHLIST_SCHEMA });
        // re-validate the same fields _newWatchlist guards, falling back to the prior values, so a
        // bad patch can't orphan the record (foreign profile) or store junk/large data as a spec.
        if (!_isProfile(found.profile)) found.profile = w.profile;
        if (!_isSource(found.source)) found.source = w.source;
        if (typeof found.original_query !== "string" && found.original_query !== null) found.original_query = w.original_query;
        found.spec = _cleanSpec(found.spec);
        return found;
      });
      if (found && !_writeWatchlists(list)) return null;   // null on a failed persist
      return found;
    }
    function renameWatchlist(id, name) { return updateWatchlist(id, { name: String(name || "").trim() || "Untitled" }); }
    function setWatchlistEnabled(id, on) { return updateWatchlist(id, { enabled: !!on }); }
    function deleteWatchlist(id) {
      if (typeof id === "string" && id.indexOf("suggested:") === 0) return setWatchlistEnabled(id, false); // can't delete a template; just disable
      var list = _rawWatchlists();
      var next = list.filter(function (w) { return w.id !== id; });
      if (next.length !== list.length) _writeWatchlists(next);
      return next.length !== list.length;
    }

    // ---- matching (REUSES BATFilters; predicate handles non-spec fields) ----
    function watchlistMatches(wl, car, nowMs) {
      if (!wl || !car) return false;
      var specOk = _F ? _F.matchesFilter(car, wl.spec || {}, nowMs) : false;
      if (!specOk) return false;
      if (wl.predicate && !wl.predicate(car)) return false;
      // Radar applies only to LIVE auctions: when a clock is supplied, an ended/undated car can't match.
      if (typeof nowMs === "number") { var end = Date.parse(car.ends_at); if (isNaN(end) || end <= nowMs) return false; }
      return true;
    }
    function matchingWatchlists(car, watchlists, nowMs) {
      return (watchlists || []).filter(function (wl) { return watchlistMatches(wl, car, nowMs); });
    }
    // Human "why it matched": the watchlist name + its normalized constraints (+ predicate note).
    function matchReason(wl, car) {
      if (!wl) return "";
      var parts = (_S && _S.specConstraints) ? _S.specConstraints(wl.spec || {}).map(function (c) { return c.label || c; }) : [];
      if (wl.note) parts.push(wl.note);
      return wl.name + (parts.length ? " — " + parts.join(", ") : "");
    }

    var migrated = migrateLegacy();   // run once on construction

    return {
      SOURCES: SOURCES, WATCHLIST_SCHEMA: WATCHLIST_SCHEMA,
      _migratedCount: migrated,
      migrateLegacy: migrateLegacy,
      listWatchlists: listWatchlists, enabledWatchlists: enabledWatchlists,
      suggestedWatchlists: _suggestedWatchlists,
      saveWatchlist: saveWatchlist, updateWatchlist: updateWatchlist,
      renameWatchlist: renameWatchlist, setWatchlistEnabled: setWatchlistEnabled, deleteWatchlist: deleteWatchlist,
      watchlistMatches: watchlistMatches, matchingWatchlists: matchingWatchlists, matchReason: matchReason,
      parsePhrase: parsePhrase,
    };
  }

  var api = {
    WATCHLIST_SCHEMA: WATCHLIST_SCHEMA, SOURCES: SOURCES, SUGGESTED: SUGGESTED, KEYS: KEYS,
    parsePhrase: parsePhrase, createWatchlists: createWatchlists,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;  // node tests
  global.BATWatchlists = api;                                                 // browser
})(typeof window !== "undefined" ? window : globalThis);
