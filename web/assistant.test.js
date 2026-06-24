// Tests for web/assistant.js — run: node --test web/assistant.test.js
// MOCK fetch only — never calls a real AI model.
const { test, beforeEach } = require("node:test");
const assert = require("node:assert");
const A = require("./assistant.js");

// the cache is module-level and persists across tests; clear it so each test exercises the mock fetch.
beforeEach(() => A.clearCache());

function norm(over) {
  return Object.assign({
    auction_key: "bat:1", title: "1990 Porsche 911 Carrera", year: 1990,
    make: { slug: "porsche", name: "Porsche" },
    vehicle_identity: { canonical_make: "porsche", canonical_model: "911", confidence: "high", trim: "carrera" },
    bid: { amount: 42000, currency: "USD", status: "live" },
    flags: { no_reserve: false }, ends_at: "2026-06-25T00:00:00Z",
    listing_url: "https://bringatrailer.com/listing/x/",
    details: { miles: 60000, tmu: false, condition: ["numbers-matching"] },
    engagement: { comments: 30, watchers: 250, views: null },
    value: { fair_value: 60000, basis: "make-model-y3", deal_pct: 0.3, n_comps: 8, appreciation_pct: 0.05, identity_confidence: "high" },
    estimate: { low: 55000, high: 70000, currency: "USD", confidence: "medium", reserve_uncertainty: { reserve: true } },
    opportunity: { score: 62, confidence: "medium", tracking: "tracking_near_expected" },
    badges: ["warning"],
    // PRIVATE fields that must NEVER be sent (these live in user-state in reality; here to prove exclusion):
    notes: "SECRET private notes", max_bid: 99999, status: "bid_plan", tags: ["secret-tag"],
    inspection_findings: "private finding", decision_rationale: "private", questions: "private q",
  }, over || {});
}

const COMPS = [
  { id: 11, title: "1989 Porsche 911 Coupe", year: 1989, price: 58000, sold_ts: 1700000000 },
  { id: 12, title: "1991 Porsche 911 Targa", year: 1991, price: 64000, sold_ts: 1700000100 },
];

function validResponse(req, over) {
  return Object.assign({
    version: "1", auction_key: req.auction_key, generated_at: "2026-06-24T00:00:00Z",
    input_hash: req.input_hash, verdict_code: "near_expected",
    summary: "Tracking near the comp-derived range with steady interest.",
    reasons: [{ text: "Current bid sits inside the estimated range.", evidence: "estimate" },
              { text: "Watcher interest is healthy.", evidence: "activity" }],
    risks: [{ text: "Reserve auction — the final price may differ.", evidence: "bid" }],
    unanswered_questions: ["Full service history?"],
    seller_notes: "Seller lists recent major service.",
    suggested_posture: "watch", evidence_refs: ["estimate", "activity", "bid"],
  }, over || {});
}

// mock fetch: reads the posted request, builds a response from it. Configurable failure modes.
function mockFetch(makeResponse, opts) {
  opts = opts || {};
  return function (url, init) {
    const req = JSON.parse(init.body);
    if (opts.httpError) return Promise.resolve({ ok: false, json: () => Promise.resolve({}) });
    if (opts.badJson) return Promise.resolve({ ok: true, json: () => Promise.reject(new Error("bad json")) });
    return Promise.resolve({ ok: true, json: () => Promise.resolve(makeResponse(req)) });
  };
}

// --- buildRequest: only public data, never private (rules 4, 5) ----------------------------

test("buildRequest sends public + deterministic data and an input_hash, and NEVER private fields", () => {
  const req = A.buildRequest(norm(), COMPS);
  const wire = JSON.stringify(req);
  for (const secret of ["SECRET private notes", "99999", "bid_plan", "secret-tag", "private finding"]) {
    assert.ok(wire.indexOf(secret) === -1, "private value leaked into the request: " + secret);
  }
  for (const key of ["notes", "max_bid", "status", "tags", "inspection_findings", "decision_rationale", "questions"]) {
    assert.ok(!(key in req) && !(key in req.auction), "private key present: " + key);
  }
  assert.strictEqual(req.auction.bid.amount, 42000);                 // public current bid sent
  assert.ok(req.analysis.estimate && req.analysis.value && req.analysis.opportunity);
  assert.strictEqual(req.comps.length, 2);
  assert.ok(typeof req.input_hash === "string" && req.input_hash.length);
  assert.ok(req.untrusted_listing_text && /untrusted/.test(req.untrusted_listing_text.note));  // rule 8
  // evidence ids the model may cite include the comps + deterministic blocks
  const ids = req.evidence.map(e => e.id);
  assert.ok(ids.includes("estimate") && ids.includes("comp:11") && ids.includes("activity"));
});

test("buildRequest caps the number of comps sent (never the whole dataset)", () => {
  const many = Array.from({ length: 50 }, (_, i) => ({ id: i, title: "c" + i, year: 1990, price: 1000 + i, sold_ts: 1 }));
  assert.strictEqual(A.buildRequest(norm(), many).comps.length, A.CAP.compsSent);
});

// --- no endpoint => behaves as before (rule 16) --------------------------------------------

test("with no endpoint configured, requestBrief stays rule-based (no fetch attempted)", async () => {
  let called = false;
  const r = await A.requestBrief(norm(), COMPS, { endpoint: "", fetchImpl: () => { called = true; } });
  assert.strictEqual(r.ok, false);
  assert.strictEqual(r.source, "rule-based");
  assert.ok(/no endpoint/.test(r.reason));
  assert.strictEqual(called, false, "no network attempted without an endpoint");
});

// --- happy path: a valid response yields an AI-assisted brief -------------------------------

test("a valid response yields a sanitized AI-assisted brief that carries NO deterministic fields", async () => {
  A.clearCache();
  const r = await A.requestBrief(norm(), COMPS, { endpoint: "https://x/brief", fetchImpl: mockFetch(validResponse) });
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.source, "ai-assisted");
  assert.strictEqual(r.brief.summary.length > 0, true);
  assert.deepStrictEqual(r.brief.reasons.map(x => x.evidence), ["estimate", "activity"]);
  // the AI brief must not contain deterministic fields it could overwrite (rule 11)
  for (const k of ["value", "estimate", "opportunity", "badges", "bid", "current_bid", "confidence"]) {
    assert.ok(!(k in r.brief), "AI brief must not carry deterministic field: " + k);
  }
});

// --- every failure mode keeps the deterministic brief (rule 14) ----------------------------

test("auction_key / input_hash mismatch are rejected (stale or forged)", async () => {
  const wrongKey = await A.requestBrief(norm(), COMPS, { endpoint: "https://x", fetchImpl: mockFetch(req => validResponse(req, { auction_key: "bat:999" })) });
  assert.ok(!wrongKey.ok && /auction_key mismatch/.test(wrongKey.reason));
  const wrongHash = await A.requestBrief(norm(), COMPS, { endpoint: "https://x", fetchImpl: mockFetch(req => validResponse(req, { input_hash: "deadbeef" })) });
  assert.ok(!wrongHash.ok && /input_hash mismatch/.test(wrongHash.reason));
});

test("an unsupported extra field is rejected", async () => {
  const r = await A.requestBrief(norm(), COMPS, { endpoint: "https://x", fetchImpl: mockFetch(req => validResponse(req, { surprise: "extra" })) });
  assert.ok(!r.ok && /unsupported field/.test(r.reason));
});

test("an unsupported verdict_code or posture is rejected", async () => {
  const v = await A.requestBrief(norm(), COMPS, { endpoint: "https://x", fetchImpl: mockFetch(req => validResponse(req, { verdict_code: "BUY_NOW" })) });
  assert.ok(!v.ok && /verdict_code/.test(v.reason));
  const p = await A.requestBrief(norm(), COMPS, { endpoint: "https://x", fetchImpl: mockFetch(req => validResponse(req, { suggested_posture: "buy" })) });
  assert.ok(!p.ok && /posture/.test(p.reason));
});

test("a claim that cites an evidence id NOT supplied is rejected (rule 9 / invalid evidence)", async () => {
  const r = await A.requestBrief(norm(), COMPS, { endpoint: "https://x",
    fetchImpl: mockFetch(req => validResponse(req, { reasons: [{ text: "made up", evidence: "comp:999" }] })) });
  assert.ok(!r.ok && /evidence/.test(r.reason));
});

test("a claim citing a JS prototype key (constructor/toString/__proto__) is rejected, not treated as supplied", async () => {
  for (const ghost of ["constructor", "toString", "__proto__", "hasOwnProperty", "valueOf"]) {
    const r = await A.requestBrief(norm(), COMPS, { endpoint: "https://x",
      fetchImpl: mockFetch(req => validResponse(req, { reasons: [{ text: "fabricated", evidence: ghost }], evidence_refs: [ghost] })) });
    assert.ok(!r.ok, "prototype-key evidence '" + ghost + "' must be rejected (got ok=" + r.ok + ")");
  }
});

test("a claim with no evidence reference is rejected (rule 9)", async () => {
  const r = await A.requestBrief(norm(), COMPS, { endpoint: "https://x",
    fetchImpl: mockFetch(req => validResponse(req, { reasons: [{ text: "unevidenced claim" }] })) });
  assert.ok(!r.ok);
});

test("a non-object / missing-summary / non-JSON response is rejected", async () => {
  const notObj = await A.requestBrief(norm(), COMPS, { endpoint: "https://x", fetchImpl: mockFetch(() => "just a string") });
  assert.ok(!notObj.ok);
  const noSummary = await A.requestBrief(norm(), COMPS, { endpoint: "https://x", fetchImpl: mockFetch(req => validResponse(req, { summary: "" })) });
  assert.ok(!noSummary.ok && /summary/.test(noSummary.reason));
  const badJson = await A.requestBrief(norm(), COMPS, { endpoint: "https://x", fetchImpl: mockFetch(validResponse, { badJson: true }) });
  assert.ok(!badJson.ok && /unavailable/.test(badJson.reason));
  const httpErr = await A.requestBrief(norm(), COMPS, { endpoint: "https://x", fetchImpl: mockFetch(validResponse, { httpError: true }) });
  assert.ok(!httpErr.ok && /unavailable/.test(httpErr.reason));
});

test("a timeout keeps the deterministic brief", async () => {
  const hanging = (url, init) => new Promise((_, reject) => {
    if (init.signal) init.signal.addEventListener("abort", () => { const e = new Error("aborted"); e.name = "AbortError"; reject(e); });
  });
  const r = await A.requestBrief(norm(), COMPS, { endpoint: "https://x", fetchImpl: hanging, timeoutMs: 20 });
  assert.ok(!r.ok && /timed out/.test(r.reason));
});

// --- size caps (rule 7) --------------------------------------------------------------------

test("oversized strings and arrays are capped", async () => {
  A.clearCache();
  const huge = req => validResponse(req, {
    summary: "x".repeat(5000),
    reasons: Array.from({ length: 50 }, () => ({ text: "y".repeat(2000), evidence: "bid" })),
    unanswered_questions: Array.from({ length: 50 }, () => "q".repeat(900)),
  });
  const r = await A.requestBrief(norm(), COMPS, { endpoint: "https://x", fetchImpl: mockFetch(huge) });
  assert.ok(r.ok);
  assert.ok(r.brief.summary.length <= A.CAP.summary);
  assert.ok(r.brief.reasons.length <= A.CAP.reasons);
  assert.ok(r.brief.reasons.every(x => x.text.length <= A.CAP.reason));
  assert.ok(r.brief.unanswered_questions.length <= A.CAP.questions);
});

// --- caching (rule 12) ---------------------------------------------------------------------

test("an identical input is served from cache (one network call)", async () => {
  A.clearCache();
  let calls = 0;
  const counting = (url, init) => { calls++; const req = JSON.parse(init.body); return Promise.resolve({ ok: true, json: () => Promise.resolve(validResponse(req)) }); };
  const a = await A.requestBrief(norm(), COMPS, { endpoint: "https://x", fetchImpl: counting });
  const b = await A.requestBrief(norm(), COMPS, { endpoint: "https://x", fetchImpl: counting });
  assert.ok(a.ok && b.ok);
  assert.strictEqual(calls, 1, "second identical request is cached");
  assert.strictEqual(b.cached, true);
});
