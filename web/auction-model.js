// web/auction-model.js — backward-compatible normalized auction model.
//
// A thin, ADDITIVE normalization layer shared by the browser and Node tests (same IIFE +
// CommonJS pattern as web/map-data.js / web/filters.js). It gives live auctions and historical
// comps one shared shape — a marketplace-qualified key, a live/sold status, a normalized
// vehicle identity, and an (as-yet-uncomputed) analysis block — WITHOUT removing or rewriting
// any existing raw field. A legacy record that carries none of the new blocks normalizes fine:
// what's missing is reported honestly as null/ambiguous, never invented or coerced to zero.
//
// Stage 1 only DEFINES and NORMALIZES these blocks. It does not compute scores, draw badges,
// change map rendering, or rewrite data/auctions.json — later stages do that.
(function (global) {
  "use strict";

  // The only marketplace today. Kept as a constant so auction keys are forward-compatible the
  // day a second source appears (e.g. "cb:" / "pcar:") without reshaping anything.
  var DEFAULT_MARKETPLACE = "bat";

  function _isObject(v) {
    return v != null && typeof v === "object" && !Array.isArray(v);
  }

  // A real number (including 0) passes through; null / undefined / NaN / non-number become null.
  // This is the load-bearing rule for the whole model: missing is NEVER 0 (a real 0 is a value).
  function numberOrNull(v) {
    return (typeof v === "number" && isFinite(v)) ? v : null;   // isFinite also rejects NaN/Infinity
  }

  function stringOrNull(v) {
    return (typeof v === "string" && v.trim() !== "") ? v : null;
  }

  // make/model can arrive three ways: a {name,slug} object (live board), a bare slug string
  // (a comp record), or inside an explicit vehicle_identity block. Normalize all to
  // { slug, name } | null. An all-null/empty input yields null — we never fabricate an identity.
  function _identityPart(v) {
    if (typeof v === "string") {
      var s = v.trim();
      return s ? { slug: s.toLowerCase(), name: s } : null;
    }
    if (_isObject(v)) {
      var slug = stringOrNull(v.slug);
      var name = stringOrNull(v.name);
      if (slug == null && name == null) return null;
      var base = slug != null ? slug : name;
      return { slug: base ? base.toLowerCase() : null, name: name != null ? name : slug };
    }
    return null;
  }

  // "<marketplace>:<id>", e.g. "bat:115717336". A usable id is a finite number or a non-empty
  // string; anything else — null, "", NaN, Infinity, an object, an array — can't key a record (it
  // can't be deduped or enriched, same rule the scraper validation uses), so we return null rather
  // than stringifying junk into a bogus, collision-prone key (e.g. every object -> "[object Object]").
  function auctionKey(platform, id) {
    var keyable = (typeof id === "number" && isFinite(id)) || (typeof id === "string" && id.trim() !== "");
    if (!keyable) return null;
    var p = (platform == null || String(platform).trim() === "")
      ? DEFAULT_MARKETPLACE
      : String(platform).trim().toLowerCase();
    return p + ":" + String(id);
  }

  // Explicit marketplace on the record wins; then snapshot metadata; then the default.
  function normalizeMarketplace(rawMarketplace, snapshotMetadata) {
    var explicit = stringOrNull(rawMarketplace);
    if (explicit) return explicit.trim().toLowerCase();
    if (snapshotMetadata && stringOrNull(snapshotMetadata.marketplace)) {
      return snapshotMetadata.marketplace.trim().toLowerCase();
    }
    return DEFAULT_MARKETPLACE;
  }

  // Normalize a vehicle identity. An explicit, well-formed vehicle_identity block is preferred;
  // anything it doesn't supply falls back to the legacy auction's year/make/models (or a comp's
  // make/model slug). A malformed block (not an object) is ignored and the legacy fields are used
  // — absence and malformation are both valid. `ambiguous` is a derived FLAG (not a score): true
  // whenever year+make+model can't all be pinned down, so a later stage knows not to hang a
  // confident valuation on an uncertain identity.
  function normalizeVehicleIdentity(rawIdentity, legacyAuction) {
    var source = null;
    var year = null, make = null, model = null, trim = null, vin = null;

    if (_isObject(rawIdentity)) {
      source = "explicit";
      year = numberOrNull(rawIdentity.year);
      make = _identityPart(rawIdentity.make);
      var explicitModel = rawIdentity.model != null
        ? rawIdentity.model
        : (Array.isArray(rawIdentity.models) ? rawIdentity.models[0] : null);
      model = _identityPart(explicitModel);
      trim = stringOrNull(rawIdentity.trim);
      vin = stringOrNull(rawIdentity.vin);
    }

    if (_isObject(legacyAuction)) {
      if (year == null) year = numberOrNull(legacyAuction.year);
      if (make == null) make = _identityPart(legacyAuction.make);
      if (model == null) {
        var legacyModel = Array.isArray(legacyAuction.models)
          ? legacyAuction.models[0]
          : (legacyAuction.model != null ? legacyAuction.model : null);
        model = _identityPart(legacyModel);
      }
      if (trim == null) trim = stringOrNull(legacyAuction.trim);
      if (vin == null) vin = stringOrNull(legacyAuction.vin);
      if (source == null && (year != null || make != null || model != null)) source = "legacy";
    }

    return {
      year: year,
      make: make,                 // { slug, name } | null
      model: model,               // { slug, name } | null
      trim: trim,
      vin: vin,
      source: source,             // "explicit" | "legacy" | null
      ambiguous: !(year != null && make != null && model != null),
    };
  }

  // Normalize an optional analysis block. Absence is valid -> null (NOT an object full of zeros).
  // A malformed block (not an object) is also treated as absent here -> null; the scraper emits a
  // soft warning for it. Present fields are sanitized: `score` is null when unknown and NEVER
  // coerced from a missing value to 0 (a real 0 is preserved). Scores are not computed in Stage 1.
  function normalizeAnalysis(rawAnalysis) {
    if (!_isObject(rawAnalysis)) return null;
    return {
      score: numberOrNull(rawAnalysis.score),
      confidence: stringOrNull(rawAnalysis.confidence),
      summary: stringOrNull(rawAnalysis.summary),
      basis: stringOrNull(rawAnalysis.basis),
      flags: Array.isArray(rawAnalysis.flags)
        ? rawAnalysis.flags.filter(function (f) { return typeof f === "string"; })
        : null,
      updated_at: stringOrNull(rawAnalysis.updated_at),
    };
  }

  // Shared tail: layer the normalized keys on top of the raw record without dropping any raw
  // field. The normalized keys win over any same-named raw key (so the sanitized vehicle_identity
  // / analysis replace a raw one), but every other raw field — id, title, bid, value, engagement,
  // details, price, sold_ts, … — is preserved verbatim for existing consumers.
  //
  // `status` and `analysis` are passed IN by the caller, not read from raw: the live/sold axis is
  // true by construction (which entry point was called), so an untrusted raw `historical_status`
  // string can never flip a live reserve auction to "sold" — which would feed the "reserve bids
  // are not sale prices" invariant a false sale. Likewise a comp passes analysis=null explicitly.
  function _normalize(raw, marketplace, status, analysis) {
    var normalized = {
      auction_key: auctionKey(marketplace, raw.id),
      marketplace: marketplace,
      historical_status: status,
      vehicle_identity: normalizeVehicleIdentity(raw.vehicle_identity, raw),
      analysis: analysis,
    };
    return Object.assign({}, raw, normalized);
  }

  // A LIVE board auction. snapshotMetadata is the snapshot's top-level object (scraped_at, source,
  // optional marketplace); it's optional and only used to resolve the marketplace.
  function normalizeLiveAuction(raw, snapshotMetadata) {
    if (!_isObject(raw)) return raw;   // defensive: never throw on junk input
    return _normalize(raw, normalizeMarketplace(raw.marketplace, snapshotMetadata), "live",
      normalizeAnalysis(raw.analysis));
  }

  // A HISTORICAL comp (a completed SALE). Same normalized shape as a live auction so both share one
  // keyspace; status is "sold". A comp is a settled result, not a forward-looking listing, so its
  // analysis is forced to null — a historical record can never surface an invented/confident metric
  // (CLAUDE.md invariant), even if an upstream writer mistakenly attaches an analysis block.
  function normalizeHistoricalComp(raw) {
    if (!_isObject(raw)) return raw;
    return _normalize(raw, normalizeMarketplace(raw.marketplace, null), "sold", null);
  }

  var api = {
    DEFAULT_MARKETPLACE: DEFAULT_MARKETPLACE,
    auctionKey: auctionKey,
    numberOrNull: numberOrNull,
    normalizeMarketplace: normalizeMarketplace,
    normalizeVehicleIdentity: normalizeVehicleIdentity,
    normalizeAnalysis: normalizeAnalysis,
    normalizeLiveAuction: normalizeLiveAuction,
    normalizeHistoricalComp: normalizeHistoricalComp,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;  // node tests
  global.BATAuctionModel = api;                                               // browser
})(typeof window !== "undefined" ? window : globalThis);
