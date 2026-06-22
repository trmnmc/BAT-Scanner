// Tests for web/map-data.js — run: node --test web/map-data.test.js
const test = require("node:test");
const assert = require("node:assert");
const M = require("./map-data.js");

const NOW = Date.parse("2026-06-22T00:00:00Z");
const inHours = (h) => new Date(NOW + h * 3600 * 1000).toISOString();

// minimal active car ending `h` hours from NOW with overridable fields
function car(over) {
  return Object.assign({
    id: 1,
    ends_at: inHours(24),
    bid: { amount: 10000, currency: "USD", status: "live" },
    engagement: { comments: null, views: null, watchers: null },
    flags: { no_reserve: false },
    value: null,
  }, over || {});
}

test("closing includes records with missing engagement", () => {
  const c = car({ engagement: { comments: null, watchers: null } });
  assert.strictEqual(M.hasMetric(c, "closing", NOW), true);
  assert.ok(M.getMetricValue(c, "closing", NOW) > 0, "returns positive hours remaining");
});

test("closing returns hours remaining, soonest is smallest", () => {
  assert.strictEqual(Math.round(M.getMetricValue(car({ ends_at: inHours(4) }), "closing", NOW)), 4);
  assert.strictEqual(Math.round(M.getMetricValue(car({ ends_at: inHours(120) }), "closing", NOW)), 120);
});

test("comments/watchers exclude unknown values without treating them as zero", () => {
  const unknown = car({ engagement: { comments: null, watchers: null } });
  assert.strictEqual(M.getMetricValue(unknown, "comments", NOW), null);
  assert.strictEqual(M.getMetricValue(unknown, "watchers", NOW), null);
  assert.strictEqual(M.hasMetric(unknown, "comments", NOW), false);

  const zero = car({ engagement: { comments: 0, watchers: 0 } });
  assert.strictEqual(M.getMetricValue(zero, "comments", NOW), 0, "a real 0 is a value, not unknown");
  assert.strictEqual(M.hasMetric(zero, "comments", NOW), true);

  const some = car({ engagement: { comments: 12, watchers: 340 } });
  assert.strictEqual(M.getMetricValue(some, "comments", NOW), 12);
  assert.strictEqual(M.getMetricValue(some, "watchers", NOW), 340);
});

test("zero/absent bids use the no-bid lane", () => {
  assert.strictEqual(M.getPlotBid(car({ bid: { amount: 0, currency: "USD" } })), M.NO_BID_PLOT_VALUE);
  assert.strictEqual(M.getPlotBid(car({ bid: { amount: null } })), M.NO_BID_PLOT_VALUE);
  assert.strictEqual(M.getPlotBid(car({ bid: { amount: 5000 } })), 5000);
  assert.strictEqual(M.hasRealBid(car({ bid: { amount: 0 } })), false);
  assert.strictEqual(M.hasRealBid(car({ bid: { amount: 5000 } })), true);
  assert.ok(M.NO_BID_PLOT_VALUE > 0, "no-bid lane is positive so a log axis can plot it");
});

test("ended records are excluded from the current-live set", () => {
  const ended = car({ ends_at: inHours(-2) });
  const live = car({ id: 2, ends_at: inHours(2) });
  assert.strictEqual(M.isActiveAuction(ended, NOW), false);
  assert.strictEqual(M.isActiveAuction(live, NOW), true);
  const active = M.getActiveAuctions([ended, live], NOW);
  assert.deepStrictEqual(active.map((c) => c.id), [2]);
  assert.strictEqual(M.getMetricValue(ended, "closing", NOW), null, "ended has no closing value");
});

test("invalid/missing ends_at is not active", () => {
  assert.strictEqual(M.isActiveAuction(car({ ends_at: null }), NOW), false);
  assert.strictEqual(M.isActiveAuction(car({ ends_at: "not-a-date" }), NOW), false);
});

test("marker mode flips at the required thresholds", () => {
  // desktop: photos at <=80, dots above
  assert.strictEqual(M.chooseMarkerMode(80, false), "photos");
  assert.strictEqual(M.chooseMarkerMode(81, false), "dots");
  // mobile: photos at <=30, dots above
  assert.strictEqual(M.chooseMarkerMode(30, true), "photos");
  assert.strictEqual(M.chooseMarkerMode(31, true), "dots");
  // full board always dots
  assert.strictEqual(M.chooseMarkerMode(1100, false), "dots");
  assert.strictEqual(M.chooseMarkerMode(1100, true), "dots");
});

test("deal mode rejects thin comps and reserve auctions", () => {
  const trusted = (over) => car(Object.assign({
    flags: { no_reserve: true },
    value: { deal_pct: 0.2, basis: "make-model-y3", n_comps: 8 },
  }, over));
  assert.ok(M.getMetricValue(trusted(), "deal", NOW) != null, "no-reserve + trusted basis qualifies");

  // reserve auction: no number even with a trusted basis
  assert.strictEqual(M.getMetricValue(trusted({ flags: { no_reserve: false } }), "deal", NOW), null);
  // thin comps: insufficient basis is rejected
  assert.strictEqual(M.getMetricValue(trusted({ value: { deal_pct: 0.3, basis: "insufficient", n_comps: 2 } }), "deal", NOW), null);
  // no value at all
  assert.strictEqual(M.getMetricValue(trusted({ value: null }), "deal", NOW), null);
  // y7 basis is trusted
  assert.ok(M.getMetricValue(trusted({ value: { deal_pct: 0.18, basis: "make-model-y7", n_comps: 6 } }), "deal", NOW) != null);
});

test("one input auction produces no more than one chart point", () => {
  // same id appearing twice (e.g. legacy multi-category) collapses to one point
  const dup = [car({ id: 7 }), car({ id: 7 })];
  assert.strictEqual(M.buildPoints(dup, "closing", NOW).length, 1);

  // a mix: active+has-metric counts once; ended and metric-less are dropped
  const cars = [
    car({ id: 1, ends_at: inHours(5) }),                                  // closing: yes
    car({ id: 2, ends_at: inHours(-1) }),                                 // ended: no
    car({ id: 3, ends_at: inHours(5), engagement: { comments: null } }),  // closing: yes
  ];
  const pts = M.buildPoints(cars, "closing", NOW);
  assert.strictEqual(pts.length, 2);
  assert.deepStrictEqual(pts.map((p) => p.id).sort(), [1, 3]);
});

test("buildPoints marks no-bid points and excludes metric-less cars", () => {
  const cars = [
    car({ id: 1, bid: { amount: 0 } }),                                   // no bid, still active+closing
    car({ id: 2, engagement: { comments: null }, value: null }),         // no comments
  ];
  const closing = M.buildPoints(cars, "closing", NOW);
  assert.strictEqual(closing.length, 2, "closing places both (bid amount irrelevant)");
  assert.strictEqual(closing.find((p) => p.id === 1).noBid, true);

  const comments = M.buildPoints(cars, "comments", NOW);
  assert.strictEqual(comments.length, 0, "neither has comment data");
});

test("getEngagementFreshness: stale vs fresh vs legacy fallback", () => {
  const base = { ends_at: inHours(10), engagement: { comments: 5, watchers: 9 } };
  // no enrichment block, no scrapedAt -> hasData but no timestamp
  assert.strictEqual(M.getEngagementFreshness(base, NOW).hasData, true);

  // fresh stamp (1h ago)
  const fresh = Object.assign({}, base, { enrichment: { engagement_updated_at: new Date(NOW - 3600 * 1000).toISOString() } });
  assert.strictEqual(M.getEngagementFreshness(fresh, NOW).stale, false);

  // stale stamp (96h ago > 72h)
  const stale = Object.assign({}, base, { enrichment: { engagement_updated_at: new Date(NOW - 96 * 3600 * 1000).toISOString() } });
  assert.strictEqual(M.getEngagementFreshness(stale, NOW).stale, true);

  // legacy record (no enrichment stamp) falls back to snapshot scraped_at
  const legacyScrapedAt = new Date(NOW - 100 * 3600 * 1000).toISOString();
  const f = M.getEngagementFreshness(base, NOW, legacyScrapedAt);
  assert.strictEqual(f.stale, true, "legacy uses scraped_at as the timestamp");

  // no engagement at all -> hasData false
  assert.strictEqual(M.getEngagementFreshness(car({ engagement: { comments: null, watchers: null } }), NOW).hasData, false);
});

test("compactNumber", () => {
  assert.strictEqual(M.compactNumber(30), "30");
  assert.strictEqual(M.compactNumber(999), "999");
  assert.strictEqual(M.compactNumber(1000), "1k");
  assert.strictEqual(M.compactNumber(1500), "1.5k");
  assert.strictEqual(M.compactNumber(24500), "24.5k");
  assert.strictEqual(M.compactNumber(1200000), "1.2M");
  assert.strictEqual(M.compactNumber(null), "");
});

test("dealTier: under / fair / over, and unknown for thin comps or reserve", () => {
  const mk = (dp, basis, nr) => car({ flags: { no_reserve: nr }, value: { deal_pct: dp, basis: basis } });
  assert.strictEqual(M.dealTier(mk(0.2, "make-model-y3", true)), "under");
  assert.strictEqual(M.dealTier(mk(-0.2, "make-model-y3", true)), "over");
  assert.strictEqual(M.dealTier(mk(0.0, "make-model-y3", true)), "fair");
  assert.strictEqual(M.dealTier(mk(0.03, "make-model-y7", true)), "fair");   // within +/-5%
  assert.strictEqual(M.dealTier(mk(0.5, "insufficient", true)), "unknown");  // thin comps
  assert.strictEqual(M.dealTier(mk(0.5, "make-model-y3", false)), "unknown"); // reserve
  assert.strictEqual(M.dealTier(car({ value: null })), "unknown");
});

test("formatDurationHours ticks", () => {
  assert.strictEqual(M.formatDurationHours(0.5), "30m");
  assert.strictEqual(M.formatDurationHours(4), "4h");
  assert.strictEqual(M.formatDurationHours(24), "1d");
  assert.strictEqual(M.formatDurationHours(120), "5d");
});

test("clampNum clamps and passes through nulls", () => {
  assert.strictEqual(M.clampNum(1.5, -0.5, 0.9), 0.9);
  assert.strictEqual(M.clampNum(-2, -0.5, 0.9), -0.5);
  assert.strictEqual(M.clampNum(0.3, -0.5, 0.9), 0.3);
  assert.strictEqual(M.clampNum(null, 0, 1), null);
  assert.strictEqual(M.clampNum(NaN, 0, 1), null, "NaN normalizes to null so it never reaches a scale");
});

test("watcherSize: sqrt scale clamped to 6-26, null -> 8", () => {
  assert.strictEqual(M.watcherSize(null), 8);
  assert.strictEqual(M.watcherSize(150), 6, "domain floor -> min size");
  assert.strictEqual(M.watcherSize(100), 6, "below floor clamps the ratio, never NaN");
  assert.strictEqual(M.watcherSize(1200), 26, "domain ceiling -> max size");
  assert.strictEqual(M.watcherSize(3000), 26, "above ceiling clamps to max");
  const mid = M.watcherSize(675);                  // ratio 0.5 -> 6 + 20*sqrt(.5) ~= 20.14
  assert.ok(mid > 19.5 && mid < 20.7, "midpoint sits high on the sqrt curve, got " + mid);
});

test("dealColorAt hits the exact palette stops and clamps the ends", () => {
  assert.strictEqual(M.dealColorAt(-0.5), "#d9534f");
  assert.strictEqual(M.dealColorAt(0), "#9aa0a8");
  assert.strictEqual(M.dealColorAt(0.49), "#6fcf97");
  assert.strictEqual(M.dealColorAt(0.9), "#1e9e5a");
  assert.strictEqual(M.dealColorAt(2.0), "#1e9e5a", "above domain clamps to deep green");
  assert.strictEqual(M.dealColorAt(-3.0), "#d9534f", "below domain clamps to red");
  assert.strictEqual(M.dealColorAt(null), M.DEAL_NULL_COLOR, "null is the fixed neutral, not an extreme");
});

test("dealPaletteColors: low->high samples, ends match the stops", () => {
  const c = M.dealPaletteColors(20);
  assert.strictEqual(c.length, 21);
  assert.strictEqual(c[0], "#d9534f", "index 0 = min value (-0.5) = red");
  assert.strictEqual(c[c.length - 1], "#1e9e5a", "last = max value (0.9) = deep green");
});

test("clampNum winsorizes price to the fixed $1k-$1M band", () => {
  // the X-axis winsor reuses clampNum(amount, 1000, 1_000_000)
  assert.strictEqual(M.clampNum(300, 1000, 1000000), 1000, "a $300 bid pins UP to the $1k floor");
  assert.strictEqual(M.clampNum(2500000, 1000, 1000000), 1000000, "a $2.5M bid pins DOWN to the $1M ceiling");
  assert.strictEqual(M.clampNum(42000, 1000, 1000000), 42000, "a bid inside the band is untouched");
});

test("dealPctLabel: under / over / at comps / unknown", () => {
  assert.strictEqual(M.dealPctLabel(0.49), "49% under");
  assert.strictEqual(M.dealPctLabel(-0.068), "7% over");
  assert.strictEqual(M.dealPctLabel(0), "at comps");
  assert.strictEqual(M.dealPctLabel(null), "—");
});
