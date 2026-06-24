// Tests for web/badges.js — run: node --test web/badges.test.js
const test = require("node:test");
const assert = require("node:assert");
const B = require("./badges.js");

const codesOf = (sel) => sel.map((b) => b.code);

// --- validate badge codes ----------------------------------------------------------------

test("isValidBadge accepts the six known codes and rejects everything else", () => {
  for (const c of ["opportunity", "hot", "trophy", "historical", "watchlist", "warning"]) {
    assert.strictEqual(B.isValidBadge(c), true, c);
  }
  for (const c of ["bogus", "", null, undefined, 42, {}, "Opportunity"]) {
    assert.strictEqual(B.isValidBadge(c), false, String(c));
  }
});

// --- readable labels and reasons ---------------------------------------------------------

test("badgeLabel / badgeGlyph / badgeReason return readable values, null for unknown", () => {
  assert.strictEqual(B.badgeLabel("opportunity"), "Opportunity");
  assert.strictEqual(B.badgeGlyph("historical"), "👻");
  assert.ok(B.badgeReason("warning") && B.badgeReason("warning").length > 0);
  assert.strictEqual(B.badgeLabel("bogus"), null);
  assert.strictEqual(B.badgeReason("bogus"), null);
});

// --- choose the highest-priority badge + limit how many display --------------------------

test("selectBadges shows at most one main + one status, highest priority each", () => {
  // opportunity(30) beats hot(20) and trophy(10) for the main slot; warning(20) beats watchlist(10)
  const sel = B.selectBadges({ bid: { status: "live" }, badges: ["hot", "trophy", "opportunity", "watchlist", "warning"] });
  assert.deepStrictEqual(codesOf(sel), ["opportunity", "warning"]);
  // exactly one main and one status, never more
  assert.strictEqual(sel.filter((b) => b.kind === "main").length, 1);
  assert.strictEqual(sel.filter((b) => b.kind === "status").length, 1);
});

test("two main badges collapse to the single highest-priority one", () => {
  const sel = B.selectBadges({ bid: { status: "live" }, badges: ["trophy", "opportunity"] });
  assert.deepStrictEqual(codesOf(sel), ["opportunity"]);
});

test("topBadge returns the single highest-priority badge, main over status", () => {
  assert.strictEqual(B.topBadge({ bid: { status: "live" }, badges: ["watchlist", "hot"] }).code, "hot");
  assert.strictEqual(B.topBadge({ bid: { status: "live" }, badges: ["warning"] }).code, "warning");
  assert.strictEqual(B.topBadge({ badges: [] }), null);
});

// --- ignore unknown badge codes ----------------------------------------------------------

test("unknown badge codes are ignored, valid ones still selected", () => {
  const sel = B.selectBadges({ bid: { status: "live" }, badges: ["bogus", "opportunity", "nope"] });
  assert.deepStrictEqual(codesOf(sel), ["opportunity"]);
});

test("a record with no badges yields an empty list (map looks unchanged)", () => {
  assert.deepStrictEqual(B.selectBadges({ bid: { status: "live" } }), []);
  assert.deepStrictEqual(B.selectBadges({}), []);
  assert.deepStrictEqual(B.selectBadges(null), []);
});

// --- Ghost is only for historical auctions -----------------------------------------------

test("Ghost (historical) shows only on a historical record, not a live one", () => {
  // live record: historical badge is dropped
  const live = B.selectBadges({ bid: { status: "live" }, badges: ["historical", "watchlist"] });
  assert.deepStrictEqual(codesOf(live), ["watchlist"]);

  // historical via bid.status sold
  const sold = B.selectBadges({ bid: { status: "sold" }, badges: ["historical", "watchlist"] });
  assert.deepStrictEqual(codesOf(sold), ["historical", "watchlist"]);

  // historical via normalized historical_status
  const hist = B.selectBadges({ historical_status: "sold", badges: ["historical"] });
  assert.deepStrictEqual(codesOf(hist), ["historical"]);
});

test("a live-only main badge does not show on a historical record, and Ghost takes the main slot", () => {
  // a sold car must not read as a live "Opportunity"; Ghost owns the main slot instead
  const sel = B.selectBadges({ bid: { status: "sold" }, badges: ["opportunity", "historical", "warning"] });
  assert.deepStrictEqual(codesOf(sel), ["historical", "warning"]);
});

// --- supplied reason/label overrides + object-form entries -------------------------------

test("object-form badge entries carry through reason/label overrides; defaults otherwise", () => {
  const sel = B.selectBadges({ bid: { status: "live" }, badges: [{ code: "opportunity", reason: "$8k under 6 comps", label: "Deal" }] });
  assert.strictEqual(sel[0].code, "opportunity");
  assert.strictEqual(sel[0].label, "Deal");
  assert.strictEqual(sel[0].reason, "$8k under 6 comps");
  // a bare code falls back to the registry label + reason
  const def = B.selectBadges({ bid: { status: "live" }, badges: ["opportunity"] })[0];
  assert.strictEqual(def.label, "Opportunity");
  assert.ok(def.reason.length > 0);
});

test("duplicate codes are de-duped", () => {
  const sel = B.selectBadges({ bid: { status: "live" }, badges: ["watchlist", "watchlist"] });
  assert.deepStrictEqual(codesOf(sel), ["watchlist"]);
});

// --- legend ------------------------------------------------------------------------------

test("legend lists all six badges with glyph + label", () => {
  const legend = B.legend();
  assert.strictEqual(legend.length, 6);
  for (const item of legend) {
    assert.ok(item.glyph && item.label && item.code, JSON.stringify(item));
  }
});
