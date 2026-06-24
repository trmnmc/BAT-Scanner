// web/badges.js — pure badge logic, shared by the browser and Node tests.
//
// Stage 2 teaches the map how to DISPLAY badges; it does NOT compute real ones. These functions
// only validate, prioritize, limit, and label badge codes that are SUPPLIED on a record (by test
// data or a future analysis block). An unknown code is ignored; a record with no badges yields an
// empty list, so the map looks and works exactly as before. Same IIFE + CommonJS pattern as
// web/map-data.js / web/auction-model.js.
(function (global) {
  "use strict";

  // Badge registry. glyph is library-free Unicode (no icon library). `kind` decides the slot a
  // badge competes for: one "main" badge + one "status" badge may show at once. `historicalOnly`
  // badges (Ghost) are eligible only on a historical record. `priority` breaks ties within a slot
  // (higher wins) — it is tunable display ordering, NOT a computed score.
  var BADGES = {
    opportunity: { code: "opportunity", label: "Opportunity", glyph: "💎", kind: "main",   priority: 30, reason: "Priced under comparable sales" },
    hot:         { code: "hot",         label: "Hot",         glyph: "🔥", kind: "main",   priority: 20, reason: "Heavy bidding and watcher interest" },
    trophy:      { code: "trophy",      label: "Trophy",      glyph: "🏆", kind: "main",   priority: 10, reason: "Standout or collector-grade example" },
    historical:  { code: "historical",  label: "Historical",  glyph: "👻", kind: "main",   priority: 25, reason: "A completed past sale", historicalOnly: true },
    watchlist:   { code: "watchlist",   label: "Watchlist",   glyph: "📡", kind: "status", priority: 10, reason: "On your watchlist" },
    warning:     { code: "warning",     label: "Warning",     glyph: "⚠️", kind: "status", priority: 20, reason: "Check the details before bidding" },
  };

  // At most one main + one status badge may display (spec rule 6).
  var MAIN_LIMIT = 1, STATUS_LIMIT = 1;

  function isValidBadge(code) {
    return typeof code === "string" && Object.prototype.hasOwnProperty.call(BADGES, code);
  }
  function badgeDef(code) { return isValidBadge(code) ? BADGES[code] : null; }
  function badgeLabel(code) { var d = badgeDef(code); return d ? d.label : null; }
  function badgeGlyph(code) { var d = badgeDef(code); return d ? d.glyph : null; }
  function badgeReason(code) { var d = badgeDef(code); return d ? d.reason : null; }

  // A historical record is a completed sale (a comp), not a live board auction. Ghost is the only
  // badge allowed on one. We read the normalized historical_status when present, and fall back to
  // the raw bid status — a live board record has neither, so Ghost never shows on the live map.
  function isHistoricalRecord(car) {
    if (!car) return false;
    var s = car.historical_status;
    if (s === "sold" || s === "historical") return true;
    return !!(car.bid && car.bid.status === "sold");
  }

  // Read SUPPLIED badges off a record (test data or a future analysis block), normalized to
  // { code, reason?, label? }. Entries may be a bare code string or an object { code, reason?,
  // label? }. Unknown/garbage entries are dropped here — the map never invents a badge.
  function readBadgeInputs(car) {
    var raw = (car && Array.isArray(car.badges)) ? car.badges
            : (car && car.analysis && Array.isArray(car.analysis.badges)) ? car.analysis.badges
            : [];
    var out = [];
    for (var i = 0; i < raw.length; i++) {
      var b = raw[i], code = null, reason, label;
      if (typeof b === "string") {
        code = b;
      } else if (b && typeof b === "object" && typeof b.code === "string") {
        code = b.code;
        if (typeof b.reason === "string") reason = b.reason;
        if (typeof b.label === "string") label = b.label;
      }
      if (isValidBadge(code)) out.push({ code: code, reason: reason, label: label });
    }
    return out;
  }

  // A display object merging any supplied reason/label override with the registry defaults.
  function toView(entry) {
    var d = BADGES[entry.code];
    return {
      code: d.code,
      label: entry.label || d.label,
      glyph: d.glyph,
      kind: d.kind,
      reason: entry.reason || d.reason,
    };
  }

  function _byPriority(a, b) { return BADGES[b.code].priority - BADGES[a.code].priority; }

  // The badges a car may DISPLAY: the highest-priority eligible main badge plus the highest-priority
  // eligible status badge — at most MAIN_LIMIT + STATUS_LIMIT. On a historical record only Ghost is
  // an eligible main; on a live record only the deal/heat mains are (so a sold car never reads as a
  // live "Opportunity", and a live car never reads as "Historical"). Status badges apply to either.
  // Unknown codes and duplicates are dropped. Returns [] when nothing qualifies.
  function selectBadges(car) {
    var inputs = readBadgeInputs(car);
    var historical = isHistoricalRecord(car);
    var mains = [], statuses = [], seen = {};
    for (var i = 0; i < inputs.length; i++) {
      var e = inputs[i], d = BADGES[e.code];
      if (seen[e.code]) continue;                 // de-dupe repeated codes
      seen[e.code] = 1;
      if (d.kind === "status") {
        statuses.push(e);
      } else if (d.kind === "main" && historical === !!d.historicalOnly) {
        mains.push(e);                            // Ghost only on historical; deal/heat mains only on live
      }
    }
    mains.sort(_byPriority);
    statuses.sort(_byPriority);
    return mains.slice(0, MAIN_LIMIT).concat(statuses.slice(0, STATUS_LIMIT)).map(toView);
  }

  // The single highest-priority badge to feature (a main badge outranks a status one), or null.
  function topBadge(car) {
    var sel = selectBadges(car);
    if (!sel.length) return null;
    for (var i = 0; i < sel.length; i++) if (sel[i].kind === "main") return sel[i];
    return sel[0];
  }

  // The full legend (one view object per registry badge), for the small on-map key.
  function legend() {
    return Object.keys(BADGES).map(function (k) { return toView({ code: k }); });
  }

  var api = {
    BADGES: BADGES,
    MAIN_LIMIT: MAIN_LIMIT,
    STATUS_LIMIT: STATUS_LIMIT,
    isValidBadge: isValidBadge,
    badgeDef: badgeDef,
    badgeLabel: badgeLabel,
    badgeGlyph: badgeGlyph,
    badgeReason: badgeReason,
    isHistoricalRecord: isHistoricalRecord,
    readBadgeInputs: readBadgeInputs,
    selectBadges: selectBadges,
    topBadge: topBadge,
    legend: legend,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;  // node tests
  global.BATBadges = api;                                                     // browser
})(typeof window !== "undefined" ? window : globalThis);
