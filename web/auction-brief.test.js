// Tests for web/auction-brief.js — run: node --test web/auction-brief.test.js
const test = require("node:test");
const assert = require("node:assert");
const BR = require("./auction-brief.js");
const A = require("./auction-model.js");

const NOW = Date.parse("2026-06-23T12:00:00Z");
const inHours = (h) => new Date(NOW + h * 3600 * 1000).toISOString();

// a normalized LIVE auction with overridable raw fields
function live(over) {
  return A.normalizeLiveAuction(Object.assign({
    id: 1, title: "1990 Porsche 911 Carrera",
    year: 1990, make: { name: "Porsche", slug: "porsche" }, models: [{ name: "911", slug: "911" }],
    bid: { amount: 42000, currency: "USD", status: "live" },
    engagement: { comments: 30, views: null, watchers: 250 },
    flags: { no_reserve: true },
    ends_at: inHours(40), listing_url: "https://bringatrailer.com/listing/p911/",
    details: { miles: 60000, tmu: false, condition: [] },
    value: { fair_value: 60000, n_comps: 8, basis: "make-model-y3", deal_pct: 0.30, is_deal: false, appreciation_pct: null },
  }, over || {}), { schema_version: 1 });
}

const allRowValues = (brief) => brief.sections.flatMap(s => s.rows.map(r => r.value));

// --- approved phrases only ----------------------------------------------------------------

test("verdict + tracking phrases always come from the approved lists", () => {
  const cases = [
    live(),
    live({ value: { fair_value: 60000, n_comps: 8, basis: "make-model-y3", deal_pct: 0.4, is_deal: true } }),
    live({ value: { fair_value: 40000, n_comps: 8, basis: "make-model-y3", deal_pct: -0.2 } }),
    live({ engagement: { comments: null, watchers: 800 }, ends_at: inHours(5) }),
    live({ value: null, engagement: { comments: null, watchers: null } }),
    live({ flags: { no_reserve: false } }),
    live({ value: { fair_value: 50000, n_comps: 3, basis: "insufficient", deal_pct: 0.1 } }),
  ];
  for (const n of cases) {
    assert.ok(BR.VERDICTS.includes(BR.verdictPhrase(n, NOW)), "verdict approved: " + BR.verdictPhrase(n, NOW));
    assert.ok(BR.TRACKINGS.includes(BR.trackingPhrase(n)), "tracking approved: " + BR.trackingPhrase(n));
    const ip = BR.interestPhrase(n);
    assert.ok(ip === null || BR.TRACKINGS.includes(ip), "interest approved or null: " + ip);
  }
});

test("verdict picks the expected phrase for clear cases", () => {
  assert.strictEqual(BR.verdictPhrase(live({ value: { fair_value: 60000, n_comps: 8, basis: "make-model-y3", deal_pct: 0.4, is_deal: true } }), NOW), "Potentially below expected range");
  assert.strictEqual(BR.verdictPhrase(live({ value: null, engagement: { comments: null, watchers: null } }), NOW), "Too little data yet");
  assert.strictEqual(BR.verdictPhrase(live({ engagement: { comments: 5, watchers: 800 }, ends_at: inHours(5), value: { fair_value: 60000, n_comps: 8, basis: "make-model-y3", deal_pct: 0.0 } }), NOW), "Looks like a bidding-war candidate");
  assert.strictEqual(BR.verdictPhrase(live({ value: { fair_value: 40000, n_comps: 8, basis: "make-model-y3", deal_pct: -0.2 } }), NOW), "Interesting, but likely expensive");
});

// --- explicit states ----------------------------------------------------------------------

test("explicit states fire correctly", () => {
  assert.strictEqual(BR.estimateState(live({ value: null })), "Too early to estimate");
  assert.strictEqual(BR.estimateState(live({ value: { fair_value: 50000, n_comps: 3, basis: "insufficient" } })), "Low confidence estimate");
  assert.strictEqual(BR.estimateState(live()), null);
  assert.strictEqual(BR.activityState(live({ engagement: { comments: null, watchers: null } })), "Activity not yet observed");
  assert.strictEqual(BR.activityState(live()), null);
  assert.strictEqual(BR.historyState(live({ value: { fair_value: null, n_comps: 0 } })), "History not yet available");
  assert.strictEqual(BR.historyState(live()), null);
});

test("a reserve auction never quotes a confident discount", () => {
  const n = live({ flags: { no_reserve: false } });
  assert.strictEqual(BR.quotableDealPct(n), null);
  const brief = BR.buildBrief(n, { nowMs: NOW });
  const est = brief.sections.find(s => s.id === "estimate");
  assert.ok(!est.rows.some(r => /under comps|over comps/.test(r.value)), "no confident discount on a reserve auction");
});

test("a raw is_deal does NOT promote the confident verdict on a reserve / untrusted-basis auction", () => {
  // reserve + trusted basis + is_deal:true -> must NOT claim "below expected range" (it's not a real price)
  const reserve = live({ flags: { no_reserve: false }, value: { fair_value: 60000, n_comps: 8, basis: "make-model-y3", deal_pct: 0.4, is_deal: true } });
  assert.notStrictEqual(BR.verdictPhrase(reserve, NOW), "Potentially below expected range");
  assert.notStrictEqual(BR.interestPhrase(reserve), "Potential opportunity", "no 'Potential opportunity' on a reserve auction");
  // no-reserve + untrusted basis + is_deal:true -> also gated (low-confidence estimate)
  const untrusted = live({ value: { fair_value: 50000, n_comps: 3, basis: "insufficient", deal_pct: 0.1, is_deal: true } });
  assert.notStrictEqual(BR.verdictPhrase(untrusted, NOW), "Potentially below expected range");
  assert.strictEqual(BR.estimateState(untrusted), "Low confidence estimate");
  // the gate still PASSES a genuine no-reserve + trusted-basis deal
  const realDeal = live({ value: { fair_value: 60000, n_comps: 8, basis: "make-model-y3", deal_pct: 0.4, is_deal: true } });
  assert.strictEqual(BR.verdictPhrase(realDeal, NOW), "Potentially below expected range");
});

test("an ambiguous vehicle identity never receives a confident valuation", () => {
  // trusted basis + no-reserve, but year/make/model can't be pinned down -> ambiguous
  const amb = BR.buildBrief(
    A.normalizeLiveAuction({ id: 5, title: "Mystery 911?", model: "911", bid: { amount: 42000, status: "live" }, flags: { no_reserve: true },
      value: { fair_value: 60000, n_comps: 8, basis: "make-model-y3", deal_pct: 0.4, is_deal: true } }, {}),
    { nowMs: NOW });
  const norm = A.normalizeLiveAuction({ id: 5, model: "911", bid: { status: "live" }, flags: { no_reserve: true },
    value: { fair_value: 60000, n_comps: 8, basis: "make-model-y3", deal_pct: 0.4, is_deal: true } }, {});
  assert.strictEqual(BR.quotableDealPct(norm), null, "no confident discount on an ambiguous identity");
  assert.strictEqual(BR.estimateState(norm), "Low confidence estimate");
  assert.notStrictEqual(BR.verdictPhrase(norm, NOW), "Potentially below expected range");
  // no "% under comps" appears anywhere in the brief
  const vals = amb.sections.flatMap(s => s.rows.map(r => r.value));
  assert.ok(!vals.some(v => /under comps|over comps/.test(v)), "no quoted discount for an ambiguous identity");
  // but the risk is still surfaced
  assert.ok(amb.sections.find(s => s.id === "risks").rows.some(r => /identity not fully confirmed/i.test(r.value)));
});

// --- present-only facts, never undefined/NaN/empty/false-zero -----------------------------

test("buildBrief shows only present facts and never emits undefined/NaN/empty", () => {
  const sparse = A.normalizeLiveAuction({ id: 2, title: "Mystery Lot", bid: { status: "live" }, flags: {} }, {});
  const brief = BR.buildBrief(sparse, { nowMs: NOW });
  for (const v of allRowValues(brief)) {
    assert.ok(typeof v === "string" && v.trim() !== "", "row value is a non-empty string: " + JSON.stringify(v));
    assert.ok(!/undefined|NaN/.test(v), "no undefined/NaN leaked: " + v);
  }
  // missing engagement -> no Watchers/Comments rows, activity state instead
  const act = brief.sections.find(s => s.id === "activity");
  assert.strictEqual(act.state, "Activity not yet observed");
  assert.ok(!act.rows.some(r => r.label === "Watchers"));
  // missing estimate -> too-early state, no median row
  assert.strictEqual(brief.sections.find(s => s.id === "estimate").state, "Too early to estimate");
});

test("a real zero engagement value is shown (not dropped), a missing one is omitted", () => {
  const zero = BR.buildBrief(live({ engagement: { comments: 0, watchers: 0 } }), { nowMs: NOW });
  const act = zero.sections.find(s => s.id === "activity");
  assert.ok(act.rows.some(r => r.label === "Comments" && r.value === "0"), "real 0 comments shown");
  const missing = BR.buildBrief(live({ engagement: { comments: null, watchers: 5 } }), { nowMs: NOW });
  const act2 = missing.sections.find(s => s.id === "activity");
  assert.ok(!act2.rows.some(r => r.label === "Comments"), "missing comments omitted");
  assert.ok(act2.rows.some(r => r.label === "Watchers" && r.value === "5"));
});

// --- placeholders never fabricate ---------------------------------------------------------

test("placeholders are present as labels but carry no value", () => {
  const brief = BR.buildBrief(live(), { nowMs: NOW });
  const ph = brief.sections.flatMap(s => s.placeholders);
  for (const label of ["Estimated final range", "Bid velocity", "Comment velocity", "Opportunity Score", "AI verdict"]) {
    assert.ok(ph.includes(label), "placeholder present: " + label);
  }
  // placeholders are plain string labels, never a fabricated number
  assert.ok(ph.every(p => typeof p === "string"));
});

// --- the 9 sections + header facts ---------------------------------------------------------

test("brief has all nine sections and the always-on header facts", () => {
  const brief = BR.buildBrief(live(), { nowMs: NOW, freshnessLabel: "2h ago" });
  assert.deepStrictEqual(brief.sections.map(s => s.id),
    ["summary", "estimate", "opportunity", "comps", "activity", "facts", "risks", "seller", "plan"]);
  assert.strictEqual(brief.header.reserveText, "No reserve");
  assert.ok(brief.header.endsAtMs > NOW, "deadline carried for the live countdown");
  assert.ok(/updated 2h ago/.test(brief.header.bidNote));
  assert.strictEqual(brief.deepLink, "https://bringatrailer.com/listing/p911/");
});

// --- historical comp -> simplified, clearly historical -------------------------------------

test("a historical comp builds a simplified, clearly-historical brief", () => {
  const comp = A.normalizeHistoricalComp({ id: 9, title: "1974 Ford Bronco", make: "ford", model: "bronco", year: 1974, price: 76500, sold_ts: 1781980318 });
  const brief = BR.buildBrief(comp, { nowMs: NOW });
  assert.strictEqual(brief.historical, true);
  assert.strictEqual(brief.verdict, null, "no live verdict on a past sale");
  assert.strictEqual(brief.header.label, "Past sale");
  assert.deepStrictEqual(brief.sections.map(s => s.id), ["sale"]);
  const sale = brief.sections[0];
  assert.ok(sale.rows.some(r => r.label === "Sold price" && /76,500/.test(r.value)));
  assert.ok(sale.rows.some(r => r.label === "Year" && r.value === "1974"));
  for (const v of allRowValues(brief)) assert.ok(!/undefined|NaN/.test(v));
});
