// web/user-state.js — profiles + per-auction personal state, and the shared localStorage store-core.
//
// Split out of the former web/storage.js (Stage 5 refactor). This module owns:
//   - the versioned, JSON-guarded, corruption-recovering store-core (read/write/memoryBackend/meta)
//     that web/watchlists.js ALSO builds on, so there is exactly ONE persistence layer, not two;
//   - profile support (me / dad / shared) and the active-profile setting;
//   - auction_user_state: a profile-isolated per-auction record (status, notes, tags, a max-bid
//     budget placeholder, last_viewed) persisted under bat_user_state_v1;
//   - a one-time, NON-LOSSY migration of the old status enum to the new one.
//
// Same IIFE + CommonJS pattern as the other web/*.js modules. All reads are JSON-guarded and recover
// from corruption (a bad blob falls back to empty WITHOUT erasing other valid keys). Large auction
// records are never stored — only small, whitelisted user state. No network, ever.
(function (global) {
  "use strict";

  var STORAGE_VERSION = 1;
  var PROFILES = ["me", "dad", "shared"];
  var DEFAULT_PROFILE = "me";

  // The NEW status enum (Stage 5 refactor). The old values are migrated IN, never lost (see
  // STATUS_MIGRATE + migrateStatusEnum). STATUS_LABELS is for any UI that surfaces the state.
  var STATUSES = ["none", "watch", "research", "bid_plan", "pass"];
  var STATUS_LABELS = { none: "—", watch: "Watching", research: "Researching", bid_plan: "Bid plan", pass: "Passed" };
  // old -> new, AND new -> new, so the same map is a safe idempotent normalizer on every read.
  // Anything not listed here is an unknown status and coerces to "none" (never stored/shown raw).
  var STATUS_MIGRATE = {
    none: "none", watch: "watch", research: "research", bid_plan: "bid_plan", pass: "pass",
    watching: "watch", researching: "research", bid_candidate: "bid_plan", passed: "pass",
  };

  var KEYS = {
    userState: "bat_user_state_v1",
    profile: "bat_active_profile_v1",
    meta: "bat_store_meta_v1",   // small shared object; user-state + watchlists each own disjoint fields
  };

  var MAX_NOTES = 2000, MAX_TAGS = 24, MAX_TAG_LEN = 48;

  function _isObj(v) { return v != null && typeof v === "object" && !Array.isArray(v); }
  function _isProfile(p) { return PROFILES.indexOf(p) !== -1; }
  // What a profile can SEE: its own records + shared. Shared sees only shared. (Reused by watchlists.)
  function visibleProfiles(p) { return p === "shared" ? ["shared"] : [p, "shared"]; }
  // Map any status (old or new) to a valid NEW status; unknown -> "none".
  function coerceStatus(s) { return STATUS_MIGRATE.hasOwnProperty(s) ? STATUS_MIGRATE[s] : "none"; }

  // ---- store-core (SHARED with watchlists.js — this is the single persistence layer) ----
  // Guarded read: a missing key -> fallback; a CORRUPT key -> fallback, and we do NOT overwrite the
  // bad blob (so a parse bug never silently erases recoverable data). Returns {ok, value}.
  function read(backend, key, fallback) {
    var raw;
    try { raw = backend.getItem(key); } catch (e) { return { ok: false, value: fallback }; }
    if (raw == null) return { ok: true, value: fallback };
    try { return { ok: true, value: JSON.parse(raw) }; }
    catch (e) { return { ok: false, value: fallback }; }   // corrupted -> recover, don't erase
  }
  function write(backend, key, value) {
    try { backend.setItem(key, JSON.stringify(value)); return true; } catch (e) { return false; }
  }

  // in-memory backend for Node tests / a localStorage-less environment.
  function memoryBackend(seed) {
    var m = Object.assign({}, seed || {});
    return {
      getItem: function (k) { return Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null; },
      setItem: function (k, v) { m[k] = String(v); },
      removeItem: function (k) { delete m[k]; },
      _dump: function () { return Object.assign({}, m); },
    };
  }

  // meta is a single small shared object ({migratedLegacy, migratedStatusEnum, suggestedEnabled, ...}).
  // Each writer read-modify-writes ONLY its own fields, so user-state and watchlists cooperate on one
  // key without a second store. Construction is sequential, so a merge never drops the other's fields.
  function readMeta(backend) { var r = read(backend, KEYS.meta, {}); return _isObj(r.value) ? r.value : {}; }
  function writeMeta(backend, patch) { return write(backend, KEYS.meta, Object.assign(readMeta(backend), patch)); }

  function createUserState(backend, opts) {
    opts = opts || {};
    backend = backend || (typeof localStorage !== "undefined" ? localStorage : memoryBackend());
    var now = typeof opts.now === "function" ? opts.now : function () { return Date.now(); };

    // ---- profiles ----
    function getActiveProfile() {
      var r = read(backend, KEYS.profile, DEFAULT_PROFILE);
      return _isProfile(r.value) ? r.value : DEFAULT_PROFILE;
    }
    function setActiveProfile(p) { if (_isProfile(p)) write(backend, KEYS.profile, p); return getActiveProfile(); }

    // ---- auction_user_state (profile-isolated; shared visible to both) ----
    function _stateMap() { var r = read(backend, KEYS.userState, {}); return _isObj(r.value) ? r.value : {}; }
    function _key(auction_key, profile) { return profile + "|" + auction_key; }
    function _default(auction_key, profile) {
      return { auction_key: auction_key, profile_id: profile, status: "none", notes: null,
        tags: [], max_bid: null, max_bid_currency: null, last_viewed: null, updated_at: 0 };
    }
    // Normalize a persisted record on READ: coerce status (old->new / unknown->none) and ensure a
    // tags array. Never mutates storage — a stale/foreign record is shown corrected, not rewritten.
    function _coerce(rec) {
      var out = Object.assign({}, rec);
      out.status = coerceStatus(rec.status);
      out.tags = Array.isArray(rec.tags) ? rec.tags.filter(function (t) { return typeof t === "string"; }) : [];
      out.last_viewed = (typeof rec.last_viewed === "number" && isFinite(rec.last_viewed)) ? rec.last_viewed : null;
      return out;
    }
    function getUserState(auction_key, profile) {
      profile = _isProfile(profile) ? profile : getActiveProfile();
      var rec = _stateMap()[_key(auction_key, profile)];
      return _isObj(rec) ? _coerce(rec) : _default(auction_key, profile);
    }
    // What `profile` SEES for a car: its own record if any, else the shared record, else none.
    function resolveUserState(auction_key, profile) {
      profile = _isProfile(profile) ? profile : getActiveProfile();
      var m = _stateMap();
      var own = m[_key(auction_key, profile)];
      if (_isObj(own)) return _coerce(own);
      var shared = m[_key(auction_key, "shared")];
      return _isObj(shared) ? _coerce(shared) : _default(auction_key, profile);
    }

    // tags: an array of short strings, deduped (case-insensitive), capped in count + length — so a
    // patch can never smuggle large/extraneous data in under "tags". `null` clears; non-array keeps prev.
    function _cleanTags(tags, prev) {
      if (tags === null) return [];
      if (!Array.isArray(tags)) return prev;
      var out = [], seen = {};
      for (var i = 0; i < tags.length && out.length < MAX_TAGS; i++) {
        var t = (typeof tags[i] === "string" ? tags[i].trim() : "").slice(0, MAX_TAG_LEN);
        var lk = t.toLowerCase();
        if (t && !seen[lk]) { seen[lk] = 1; out.push(t); }
      }
      return out;
    }

    function setUserState(auction_key, profile, patch) {
      profile = _isProfile(profile) ? profile : getActiveProfile();
      patch = patch || {};
      var m = _stateMap();
      var k = _key(auction_key, profile);
      var prev = _isObj(m[k]) ? _coerce(m[k]) : _default(auction_key, profile);
      // Whitelist to the CLOSED auction_user_state field set — an arbitrary patch (e.g. a whole car
      // record under a stray key) can never be persisted ("no large records"). Notes + tags capped.
      // A status is accepted only if it maps to a known NEW status (old enum values are accepted and
      // migrated forward); anything else falls back to the prior value.
      var mappedStatus = (typeof patch.status === "string" && STATUS_MIGRATE.hasOwnProperty(patch.status))
        ? STATUS_MIGRATE[patch.status] : null;
      var next = {
        auction_key: auction_key, profile_id: profile,
        status: mappedStatus || prev.status,
        notes: typeof patch.notes === "string" ? patch.notes.slice(0, MAX_NOTES) : (patch.notes === null ? null : prev.notes),
        tags: ("tags" in patch) ? _cleanTags(patch.tags, prev.tags) : prev.tags,
        max_bid: typeof patch.max_bid === "number" && isFinite(patch.max_bid) ? patch.max_bid : (patch.max_bid === null ? null : prev.max_bid),
        max_bid_currency: typeof patch.max_bid_currency === "string" ? patch.max_bid_currency : (patch.max_bid_currency === null ? null : prev.max_bid_currency),
        last_viewed: typeof patch.last_viewed === "number" && isFinite(patch.last_viewed) ? patch.last_viewed : (patch.last_viewed === null ? null : prev.last_viewed),
        updated_at: now(),
      };
      if (STATUSES.indexOf(next.status) === -1) next.status = "none";
      m[k] = next;
      return write(backend, KEYS.userState, m) ? next : null;   // null on a failed persist (quota/private mode)
    }

    // Record that the active profile opened this auction. Updates ONLY last_viewed (status/notes/etc.
    // are preserved) — viewing a car is not the same as watching it.
    function touchLastViewed(auction_key, profile) {
      return setUserState(auction_key, profile, { last_viewed: now() });
    }

    // ---- one-time status-enum migration ----
    // Rewrite bat_user_state_v1 mapping each record's old status to the new enum. Idempotent, and the
    // done-flag is set ONLY once the rewrite actually persisted — so a quota/private-mode failure
    // retries on the next load instead of silently leaving stale-enum records behind (blocker fix
    // mirrored from the legacy-views migration). Records are otherwise untouched; corrupt storage is a
    // no-op (read recovers to {} without erasing the bad blob). coerceStatus on read is the backstop.
    function migrateStatusEnum() {
      var meta = readMeta(backend);
      if (meta.migratedStatusEnum) return 0;
      var r = read(backend, KEYS.userState, {});
      var map = _isObj(r.value) ? r.value : {};
      var changed = 0, out = {};
      Object.keys(map).forEach(function (k) {
        var rec = map[k];
        if (!_isObj(rec)) return;                       // drop non-object junk on the rewrite
        var ns = coerceStatus(rec.status);
        if (ns !== rec.status) changed += 1;
        out[k] = Object.assign({}, rec, { status: ns });
      });
      var wrote = changed ? write(backend, KEYS.userState, out) : true;
      if (wrote) writeMeta(backend, { migratedStatusEnum: true, storageVersion: STORAGE_VERSION });
      return wrote ? changed : 0;
    }

    var migratedStatus = migrateStatusEnum();   // run once on construction

    return {
      PROFILES: PROFILES, STATUSES: STATUSES, STATUS_LABELS: STATUS_LABELS,
      STORAGE_VERSION: STORAGE_VERSION,
      _migratedStatusCount: migratedStatus,
      getActiveProfile: getActiveProfile, setActiveProfile: setActiveProfile,
      visibleProfiles: visibleProfiles,
      getUserState: getUserState, resolveUserState: resolveUserState,
      setUserState: setUserState, touchLastViewed: touchLastViewed,
    };
  }

  var api = {
    STORAGE_VERSION: STORAGE_VERSION, PROFILES: PROFILES, DEFAULT_PROFILE: DEFAULT_PROFILE,
    STATUSES: STATUSES, STATUS_LABELS: STATUS_LABELS, STATUS_MIGRATE: STATUS_MIGRATE, KEYS: KEYS,
    // store-core, shared with watchlists.js so there is exactly one persistence layer:
    read: read, write: write, memoryBackend: memoryBackend, readMeta: readMeta, writeMeta: writeMeta,
    visibleProfiles: visibleProfiles, coerceStatus: coerceStatus,
    createUserState: createUserState,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;  // node tests
  global.BATUserState = api;                                                  // browser
})(typeof window !== "undefined" ? window : globalThis);
