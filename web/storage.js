// web/storage.js — versioned localStorage persistence behind a small, swappable adapter.
//
// The adapter is intentionally tiny and backend-agnostic: createStorage(backend) takes any
// localStorage-like object ({getItem,setItem,removeItem}), so the same API can later sit on top of
// Supabase or another synchronized backend without touching callers. All reads are JSON-guarded and
// recover from corruption (a bad blob falls back to empty WITHOUT erasing other valid keys). Large
// auction records are never stored — only specs, ids, and small user state. Same IIFE + CommonJS
// pattern as the other web/*.js modules; matching REUSES BATFilters/BATSearch (no second engine).
(function (global) {
  "use strict";

  var _node = (typeof module !== "undefined" && module.exports);
  var _F = _node ? require("./filters.js") : global.BATFilters;   // matching engine (reused)
  var _S = _node ? require("./search.js") : global.BATSearch;     // spec -> human constraints (reused)

  var STORAGE_VERSION = 1;
  var WATCHLIST_SCHEMA = 1;
  var PROFILES = ["me", "dad", "shared"];
  var STATUSES = ["none", "watching", "researching", "bid_candidate", "passed"];
  var ORIGINS = ["manual", "suggested", "ai"];
  var DEFAULT_PROFILE = "me";

  var KEYS = {
    watchlists: "bat_watchlists_v1",
    userState: "bat_user_state_v1",
    profile: "bat_active_profile_v1",
    meta: "bat_store_meta_v1",
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

  function _isProfile(p) { return PROFILES.indexOf(p) !== -1; }
  function _isStatus(s) { return STATUSES.indexOf(s) !== -1; }
  function _isObj(v) { return v != null && typeof v === "object" && !Array.isArray(v); }
  function _coerceStatus(rec) { return _isStatus(rec.status) ? rec : Object.assign({}, rec, { status: "none" }); }

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

  // Guarded read: a missing key -> fallback; a CORRUPT key -> fallback, and we do NOT overwrite the
  // bad blob (so a parse bug never silently erases recoverable data). Returns {ok, value}.
  function _read(backend, key, fallback) {
    var raw;
    try { raw = backend.getItem(key); } catch (e) { return { ok: false, value: fallback }; }
    if (raw == null) return { ok: true, value: fallback };
    try { return { ok: true, value: JSON.parse(raw) }; }
    catch (e) { return { ok: false, value: fallback }; }   // corrupted -> recover, don't erase
  }
  function _write(backend, key, value) {
    try { backend.setItem(key, JSON.stringify(value)); return true; } catch (e) { return false; }
  }

  // in-memory backend for Node tests / a localStorage-less environment
  function memoryBackend(seed) {
    var m = Object.assign({}, seed || {});
    return {
      getItem: function (k) { return Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null; },
      setItem: function (k, v) { m[k] = String(v); },
      removeItem: function (k) { delete m[k]; },
      _dump: function () { return Object.assign({}, m); },
    };
  }

  function createStorage(backend, opts) {
    opts = opts || {};
    backend = backend || (typeof localStorage !== "undefined" ? localStorage : memoryBackend());
    var now = typeof opts.now === "function" ? opts.now : function () { return Date.now(); };
    var seq = 0;
    var genId = typeof opts.genId === "function" ? opts.genId
      : function () { seq += 1; return "wl_" + now().toString(36) + "_" + seq.toString(36); };

    function _meta() {
      var r = _read(backend, KEYS.meta, {});
      return _isObj(r.value) ? r.value : {};
    }
    function _setMeta(patch) { _write(backend, KEYS.meta, Object.assign(_meta(), patch)); }

    // ---- profiles ----
    function getActiveProfile() {
      var r = _read(backend, KEYS.profile, DEFAULT_PROFILE);
      return _isProfile(r.value) ? r.value : DEFAULT_PROFILE;
    }
    function setActiveProfile(p) { if (_isProfile(p)) _write(backend, KEYS.profile, p); return getActiveProfile(); }
    // What a profile can SEE: its own records + shared. Shared sees only shared.
    function _visibleProfiles(p) { return p === "shared" ? ["shared"] : [p, "shared"]; }

    // ---- raw watchlist store (manual + materialized) ----
    function _rawWatchlists() {
      var r = _read(backend, KEYS.watchlists, []);
      return Array.isArray(r.value) ? r.value.filter(_isObj) : [];
    }
    function _writeWatchlists(list) { return _write(backend, KEYS.watchlists, list); }

    // ---- legacy migration: old saved views ({name,spec}) -> manual watchlists. Idempotent, and the
    //      legacy key is preserved (never erased), so an old build keeps working too. ----
    function migrateLegacy() {
      var meta = _meta();
      if (meta.migratedLegacy) return 0;
      var r = _read(backend, KEYS.legacySaved, []);
      var legacy = Array.isArray(r.value) ? r.value : [];
      var list = _rawWatchlists();
      var existingNames = {};
      list.forEach(function (w) { if (w.origin === "manual") existingNames[w.name] = 1; });
      var added = 0;
      legacy.forEach(function (v) {
        if (!_isObj(v) || !v.name || existingNames[v.name]) return;
        list.push(_newWatchlist({ name: v.name, spec: _isObj(v.spec) ? v.spec : {}, profile_id: "shared", origin: "manual", enabled: false }));
        existingNames[v.name] = 1;
        added += 1;
      });
      // Only mark migration done once the watchlist write actually succeeded — otherwise a quota /
      // private-mode failure would set the flag, skip retry next load, and orphan the legacy views.
      var wrote = added ? _writeWatchlists(list) : true;
      if (wrote) _setMeta({ migratedLegacy: true, storageVersion: STORAGE_VERSION });
      return wrote ? added : 0;
    }

    function _newWatchlist(o) {
      var t = now();
      return {
        id: o.id || genId(),
        schema_version: WATCHLIST_SCHEMA,
        name: String(o.name || "Untitled"),
        profile_id: _isProfile(o.profile_id) ? o.profile_id : DEFAULT_PROFILE,
        origin: ORIGINS.indexOf(o.origin) !== -1 ? o.origin : "manual",
        enabled: o.enabled != null ? !!o.enabled : true,
        spec: _cleanSpec(o.spec),
        created_at: o.created_at || t,
        updated_at: t,
      };
    }

    // suggested watchlists are generated fresh from templates (predicate can't serialize); only their
    // enabled-state is persisted in meta.suggestedEnabled.
    function _suggestedWatchlists() {
      var enabled = _meta().suggestedEnabled || {};
      return SUGGESTED.map(function (t) {
        return {
          id: "suggested:" + t.key, schema_version: WATCHLIST_SCHEMA, name: t.name,
          profile_id: "shared", origin: "suggested", enabled: !!enabled[t.key],
          spec: t.spec || {}, predicate: t.predicate || null, note: t.note || null,
          created_at: 0, updated_at: 0,
        };
      });
    }

    // ---- watchlist CRUD ----
    function listWatchlists(profile, includeSuggested) {
      profile = _isProfile(profile) ? profile : getActiveProfile();
      var vis = _visibleProfiles(profile);
      var manual = _rawWatchlists().filter(function (w) { return vis.indexOf(w.profile_id) !== -1; });
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
        if (!_isProfile(found.profile_id)) found.profile_id = w.profile_id;
        if (ORIGINS.indexOf(found.origin) === -1) found.origin = w.origin;
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

    // ---- auction_user_state (profile-isolated; shared visible to both) ----
    function _stateMap() {
      var r = _read(backend, KEYS.userState, {});
      return _isObj(r.value) ? r.value : {};
    }
    function _stateKey(auction_key, profile) { return profile + "|" + auction_key; }
    function _defaultState(auction_key, profile) {
      return { auction_key: auction_key, profile_id: profile, status: "none", notes: null, max_bid: null, max_bid_currency: null, updated_at: 0 };
    }
    function getUserState(auction_key, profile) {
      profile = _isProfile(profile) ? profile : getActiveProfile();
      var m = _stateMap();
      var rec = m[_stateKey(auction_key, profile)];
      return _isObj(rec) ? _coerceStatus(rec) : _defaultState(auction_key, profile);   // coerce a stale/bad status on read
    }
    // What `profile` SEES for a car: its own record if any, else the shared record, else none.
    function resolveUserState(auction_key, profile) {
      profile = _isProfile(profile) ? profile : getActiveProfile();
      var m = _stateMap();
      var own = m[_stateKey(auction_key, profile)];
      if (_isObj(own)) return _coerceStatus(own);
      var shared = m[_stateKey(auction_key, "shared")];
      return _isObj(shared) ? _coerceStatus(shared) : _defaultState(auction_key, profile);
    }
    function setUserState(auction_key, profile, patch) {
      profile = _isProfile(profile) ? profile : getActiveProfile();
      patch = patch || {};
      var m = _stateMap();
      var k = _stateKey(auction_key, profile);
      var prev = _isObj(m[k]) ? m[k] : _defaultState(auction_key, profile);
      // Whitelist to the closed auction_user_state field set — an arbitrary patch (e.g. a whole car
      // record under a stray key) can never be persisted (enforces "no large records"). Notes capped.
      var next = {
        auction_key: auction_key, profile_id: profile,
        status: _isStatus(patch.status) ? patch.status : prev.status,
        notes: typeof patch.notes === "string" ? patch.notes.slice(0, 2000) : (patch.notes === null ? null : prev.notes),
        max_bid: typeof patch.max_bid === "number" && isFinite(patch.max_bid) ? patch.max_bid : (patch.max_bid === null ? null : prev.max_bid),
        max_bid_currency: typeof patch.max_bid_currency === "string" ? patch.max_bid_currency : (patch.max_bid_currency === null ? null : prev.max_bid_currency),
        updated_at: now(),
      };
      if (!_isStatus(next.status)) next.status = "none";
      m[k] = next;
      return _write(backend, KEYS.userState, m) ? next : null;   // null on a failed persist
    }

    var migrated = migrateLegacy();   // run once on construction

    return {
      KEYS: KEYS, PROFILES: PROFILES, STATUSES: STATUSES, ORIGINS: ORIGINS,
      STORAGE_VERSION: STORAGE_VERSION, WATCHLIST_SCHEMA: WATCHLIST_SCHEMA,
      _migratedCount: migrated,
      getActiveProfile: getActiveProfile, setActiveProfile: setActiveProfile,
      migrateLegacy: migrateLegacy,
      listWatchlists: listWatchlists, enabledWatchlists: enabledWatchlists,
      suggestedWatchlists: _suggestedWatchlists,
      saveWatchlist: saveWatchlist, updateWatchlist: updateWatchlist,
      renameWatchlist: renameWatchlist, setWatchlistEnabled: setWatchlistEnabled, deleteWatchlist: deleteWatchlist,
      watchlistMatches: watchlistMatches, matchingWatchlists: matchingWatchlists, matchReason: matchReason,
      getUserState: getUserState, resolveUserState: resolveUserState, setUserState: setUserState,
    };
  }

  var api = {
    STORAGE_VERSION: STORAGE_VERSION, PROFILES: PROFILES, STATUSES: STATUSES, ORIGINS: ORIGINS,
    SUGGESTED: SUGGESTED, KEYS: KEYS,
    createStorage: createStorage, memoryBackend: memoryBackend,
  };
  if (_node) module.exports = api;      // node tests
  global.BATStorage = api;              // browser
})(typeof window !== "undefined" ? window : globalThis);
