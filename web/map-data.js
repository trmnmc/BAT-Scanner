// web/map-data.js — pure map logic, shared by the browser and Node tests.
//
// The map shows the ENTIRE live board by default. None of this depends on the
// placeholder category tags. Time-sensitive functions take an optional nowMs so
// tests are deterministic. Same browser + CommonJS pattern as web/filters.js.
//
// Metrics:
//   closing  — hours remaining from ends_at (default). Every active auction qualifies.
//   comments — engagement.comments, only when not null (unknown is never 0).
//   watchers — engagement.watchers, only when not null (unknown is never 0).
//   deal     — value.deal_pct, only for a no-reserve auction on a trusted comp basis.
//
// Zero/no bids get a synthetic "no bid" lane on the log X axis (a positive value
// below every real bid), so they appear without faking a $1 bid.
(function (global) {
  "use strict";

  var HOUR_MS = 3600 * 1000;

  // The X axis is logarithmic and can't plot 0. A zero/absent bid is parked at a
  // positive value below every real bid; the axis formatter and tooltip relabel it.
  var NO_BID_PLOT_VALUE = 0.1;

  // Auto marker density: photos only for a small plottable set; the full board uses dots.
  var MARKER_PHOTO_MAX_DESKTOP = 80;
  var MARKER_PHOTO_MAX_MOBILE = 30;

  // Cached engagement older than this is shown faded and labelled stale.
  var STALE_AFTER_MS = 72 * HOUR_MS;

  // Comp bases trusted enough to quote a discount (mirrors filters.js SCOREABLE).
  var TRUSTED_BASES = { "make-model-y3": 1, "make-model-y7": 1 };

  function _now(nowMs) { return typeof nowMs === "number" ? nowMs : Date.now(); }

  function parseTime(v) {
    if (v == null) return NaN;
    if (typeof v === "number") return v;
    var t = Date.parse(v);
    return isNaN(t) ? NaN : t;
  }

  function endsAtMs(car) { return parseTime(car && car.ends_at); }

  // Active = has a valid ends_at that is still in the future. A record whose end
  // time has passed since the snapshot was written is no longer active.
  function isActiveAuction(car, nowMs) {
    var end = endsAtMs(car);
    if (isNaN(end)) return false;
    return end > _now(nowMs);
  }

  function getActiveAuctions(cars, nowMs) {
    var now = _now(nowMs);
    return (cars || []).filter(function (c) { return isActiveAuction(c, now); });
  }

  function _engagement(car, key) {
    var e = car && car.engagement;
    return e && e[key] != null ? e[key] : null;
  }

  function _dealValue(car) {
    var v = car && car.value;
    if (!v || v.deal_pct == null) return null;
    if (!TRUSTED_BASES[v.basis]) return null;            // thin/insufficient comps -> no number
    if (!(car.flags && car.flags.no_reserve)) return null; // reserve bid isn't a real price
    return v.deal_pct * 100;                              // percent under comps
  }

  // How the current bid compares to a trusted comp median, for coloring search matches:
  //   "under" (good buy) | "fair" | "over" | "unknown" (no trusted comps). A reserve auction
  //   or a thin/insufficient comp basis is always "unknown" — we never imply a discount we
  //   can't stand behind.
  var DEAL_TIER_MARGIN = 0.05;                           // +/-5% around the comp median = "fair"
  function dealTier(car) {
    var pct = _dealValue(car);                           // already gated on trusted basis + no_reserve
    if (pct == null) return "unknown";
    if (pct >= DEAL_TIER_MARGIN * 100) return "under";
    if (pct <= -DEAL_TIER_MARGIN * 100) return "over";
    return "fair";
  }

  // The plotted value for a metric, or null when this car can't be placed on it.
  function getMetricValue(car, metric, nowMs) {
    switch (metric) {
      case "closing":
        if (!isActiveAuction(car, nowMs)) return null;
        return (endsAtMs(car) - _now(nowMs)) / HOUR_MS;   // hours remaining (positive)
      case "comments": return _engagement(car, "comments");
      case "watchers": return _engagement(car, "watchers");
      case "deal": return _dealValue(car);
      case "marketcontext": {           // price (X) vs vehicle year (Y); year is never invented
        var y = car && car.year;
        return (typeof y === "number" && isFinite(y)) ? y : null;
      }
      default: return null;
    }
  }

  function hasMetric(car, metric, nowMs) {
    return getMetricValue(car, metric, nowMs) != null;
  }

  // Real positive bid, or the no-bid lane value for a zero/absent bid.
  function getPlotBid(car) {
    var amt = car && car.bid && car.bid.amount;
    return typeof amt === "number" && amt > 0 ? amt : NO_BID_PLOT_VALUE;
  }

  function hasRealBid(car) {
    var amt = car && car.bid && car.bid.amount;
    return typeof amt === "number" && amt > 0;
  }

  function chooseMarkerMode(plottableCount, isMobile) {
    var limit = isMobile ? MARKER_PHOTO_MAX_MOBILE : MARKER_PHOTO_MAX_DESKTOP;
    return plottableCount <= limit ? "photos" : "dots";
  }

  // Freshness of a car's cached engagement/details.
  //   { hasData, updatedAtMs, ageMs, stale, label }
  // label is a short human age ("2h ago", "3d ago") or null when there's no data.
  function getEngagementFreshness(car, nowMs, snapshotScrapedAt) {
    var hasData = _engagement(car, "comments") != null ||
                  _engagement(car, "watchers") != null ||
                  !!(car && car.details && (car.details.miles != null ||
                     (car.details.condition && car.details.condition.length)));
    if (!hasData) return { hasData: false, updatedAtMs: null, ageMs: null, stale: false, label: null };
    var stamp = car && car.enrichment && car.enrichment.engagement_updated_at;
    var updatedAtMs = parseTime(stamp);
    if (isNaN(updatedAtMs)) updatedAtMs = parseTime(snapshotScrapedAt); // legacy: fall back to scan time
    if (isNaN(updatedAtMs)) return { hasData: true, updatedAtMs: null, ageMs: null, stale: false, label: null };
    var ageMs = _now(nowMs) - updatedAtMs;
    return {
      hasData: true,
      updatedAtMs: updatedAtMs,
      ageMs: ageMs,
      stale: ageMs > STALE_AFTER_MS,
      label: formatAge(ageMs),
    };
  }

  function compactNumber(value) {
    if (value == null || isNaN(value)) return "";
    var n = Number(value), abs = Math.abs(n);
    if (abs >= 1e6) return trim(n / 1e6) + "M";
    if (abs >= 1e3) return trim(n / 1e3) + "k";
    return String(Math.round(n));
  }
  function trim(n) {
    // one decimal, but drop a trailing ".0"
    var s = n.toFixed(1);
    return s.replace(/\.0$/, "");
  }

  // Compact single-unit duration for axis ticks: "30m", "4h", "1d", "5d".
  function formatDurationHours(hours) {
    if (hours == null || isNaN(hours)) return "";
    if (hours < 0) hours = 0;
    if (hours < 1) return Math.round(hours * 60) + "m";
    if (hours < 24) return Math.round(hours) + "h";
    return Math.round(hours / 24) + "d";
  }

  function formatAge(ageMs) {
    if (ageMs == null || isNaN(ageMs)) return null;
    if (ageMs < 0) ageMs = 0;
    var mins = Math.floor(ageMs / 60000);
    if (mins < 60) return mins + "m ago";
    var hrs = Math.floor(mins / 60);
    if (hrs < 48) return hrs + "h ago";
    return Math.floor(hrs / 24) + "d ago";
  }

  // ---- Richer encoding for the Comments field (color/size/legend) -------------------
  // These are pure so they can be unit-tested outside the browser; index.html wires them
  // into the ECharts visualMap + per-point symbolSize.

  function clampNum(n, lo, hi) {
    if (n == null || isNaN(n)) return null;   // normalize null/NaN to null so it never reaches a scale
    return n < lo ? lo : (n > hi ? hi : n);
  }

  // Marker size from watcher count: sqrt so the crowded low end still separates, clamped to a
  // 6–26px range over the [150, 1200] (~p10..p95) band. Unknown watchers get a neutral 8px.
  // The input ratio is clamped to [0,1] first (the spec clamps the output, but a watcher below
  // 150 would make the sqrt argument negative → NaN; clamping the ratio is the same intent).
  var WATCHER_SIZE_MIN = 6, WATCHER_SIZE_MAX = 26, WATCHER_DOMAIN_LO = 150, WATCHER_DOMAIN_HI = 1200;
  function watcherSize(watchers) {
    if (watchers == null || isNaN(watchers)) return 8;
    var r = clampNum((watchers - WATCHER_DOMAIN_LO) / (WATCHER_DOMAIN_HI - WATCHER_DOMAIN_LO), 0, 1);
    return clampNum(WATCHER_SIZE_MIN + 20 * Math.sqrt(r), WATCHER_SIZE_MIN, WATCHER_SIZE_MAX);
  }

  // Diverging deal-quality palette as a piecewise-linear gradient over deal_pct.
  // Stops: -0.5 red (overpriced) -> 0 gray (at comps) -> 0.49 light green (median deal) -> 0.9 deep green.
  var DEAL_VMAP_MIN = -0.5, DEAL_VMAP_MAX = 0.9, DEAL_NULL_COLOR = "#6b7280";
  var DEAL_STOPS = [
    [-0.5, [217, 83, 79]],    // #d9534f
    [0.0,  [154, 160, 168]],  // #9aa0a8
    [0.49, [111, 207, 151]],  // #6fcf97
    [0.9,  [30, 158, 90]],    // #1e9e5a
  ];
  function _hex2(n) { var s = Math.round(n).toString(16); return s.length < 2 ? "0" + s : s; }
  function _rgbHex(a) { return "#" + _hex2(a[0]) + _hex2(a[1]) + _hex2(a[2]); }
  function _mix(a, b, t) { return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]; }
  // Color for a single deal_pct value (clamped to the palette domain at the ends).
  function dealColorAt(value) {
    var s = DEAL_STOPS;
    if (value == null || isNaN(value)) return DEAL_NULL_COLOR;
    if (value <= s[0][0]) return _rgbHex(s[0][1]);
    if (value >= s[s.length - 1][0]) return _rgbHex(s[s.length - 1][1]);
    for (var i = 0; i < s.length - 1; i++) {
      if (value >= s[i][0] && value <= s[i + 1][0]) {
        var t = (value - s[i][0]) / (s[i + 1][0] - s[i][0]);
        return _rgbHex(_mix(s[i][1], s[i + 1][1], t));
      }
    }
    return _rgbHex(s[s.length - 1][1]);
  }
  // Evenly-spaced color samples (low->high) for ECharts visualMap.inRange.color, which interpolates
  // its array evenly across [min,max]; sampling our non-even stops reproduces the intended curve.
  function dealPaletteColors(steps) {
    steps = steps || 20;
    var out = [];
    for (var i = 0; i <= steps; i++) {
      out.push(dealColorAt(DEAL_VMAP_MIN + (DEAL_VMAP_MAX - DEAL_VMAP_MIN) * i / steps));
    }
    return out;
  }
  // Human deal label for the tooltip: 0.49 -> "49% under", -0.07 -> "7% over", 0 -> "at comps".
  function dealPctLabel(pct) {
    if (pct == null || isNaN(pct)) return "—";
    var p = Math.round(pct * 100);
    if (p > 0) return p + "% under";
    if (p < 0) return (-p) + "% over";
    return "at comps";
  }

  // One chart point per auction id (never duplicated by category). Returns
  // [{ id, x, y, car, noBid }] for every active car that has the metric.
  function buildPoints(cars, metric, nowMs) {
    var now = _now(nowMs), seen = {}, points = [];
    var list = cars || [];
    for (var i = 0; i < list.length; i++) {
      var car = list[i];
      if (!isActiveAuction(car, now)) continue;
      var y = getMetricValue(car, metric, now);
      if (y == null) continue;
      var id = car.id;
      if (id != null) {
        if (seen[id]) continue;       // de-dupe: one point per id
        seen[id] = 1;
      }
      points.push({ id: id, x: getPlotBid(car), y: y, car: car, noBid: !hasRealBid(car) });
    }
    return points;
  }

  var api = {
    NO_BID_PLOT_VALUE: NO_BID_PLOT_VALUE,
    MARKER_PHOTO_MAX_DESKTOP: MARKER_PHOTO_MAX_DESKTOP,
    MARKER_PHOTO_MAX_MOBILE: MARKER_PHOTO_MAX_MOBILE,
    STALE_AFTER_MS: STALE_AFTER_MS,
    TRUSTED_BASES: TRUSTED_BASES,
    parseTime: parseTime,
    isActiveAuction: isActiveAuction,
    getActiveAuctions: getActiveAuctions,
    getMetricValue: getMetricValue,
    hasMetric: hasMetric,
    dealTier: dealTier,
    getPlotBid: getPlotBid,
    hasRealBid: hasRealBid,
    chooseMarkerMode: chooseMarkerMode,
    getEngagementFreshness: getEngagementFreshness,
    compactNumber: compactNumber,
    formatDurationHours: formatDurationHours,
    formatAge: formatAge,
    buildPoints: buildPoints,
    clampNum: clampNum,
    watcherSize: watcherSize,
    dealColorAt: dealColorAt,
    dealPaletteColors: dealPaletteColors,
    dealPctLabel: dealPctLabel,
    DEAL_VMAP_MIN: DEAL_VMAP_MIN,
    DEAL_VMAP_MAX: DEAL_VMAP_MAX,
    DEAL_NULL_COLOR: DEAL_NULL_COLOR,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api; // node tests
  global.BATMapData = api;                                                    // browser
})(typeof window !== "undefined" ? window : globalThis);
