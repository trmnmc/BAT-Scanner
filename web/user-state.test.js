// Tests for web/user-state.js — run: node --test web/user-state.test.js
const test = require("node:test");
const assert = require("node:assert");
const US = require("./user-state.js");

let _t = 1000;
const opts = () => ({ now: () => (_t += 1) });

// a backend whose setItem throws for chosen keys (quota / private mode); reads still work.
function flakyBackend(seed, failKeys) {
  const m = Object.assign({}, seed || {});
  const fail = new Set(failKeys || []);
  return {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null),
    setItem: (k, v) => { if (fail.has(k)) throw new Error("QuotaExceeded"); m[k] = String(v); },
    removeItem: (k) => { delete m[k]; },
    _dump: () => Object.assign({}, m),
  };
}

// --- active profile persists ---------------------------------------------------------------

test("active profile persists and rejects invalid values", () => {
  const be = US.memoryBackend();
  const a = US.createUserState(be, opts());
  assert.strictEqual(a.getActiveProfile(), "me");        // default
  a.setActiveProfile("dad");
  assert.strictEqual(US.createUserState(be, opts()).getActiveProfile(), "dad");   // persisted
  a.setActiveProfile("bogus");
  assert.strictEqual(a.getActiveProfile(), "dad");        // invalid ignored
});

// --- profile isolation + shared visibility -------------------------------------------------

test("auction_user_state is profile-isolated; shared state is visible from either profile", () => {
  const st = US.createUserState(US.memoryBackend(), opts());
  st.setUserState("bat:1", "me", { status: "watch", notes: "mine", max_bid: 50000, max_bid_currency: "USD" });
  st.setUserState("bat:1", "dad", { status: "pass", notes: "dad's" });
  st.setUserState("bat:2", "shared", { status: "bid_plan", notes: "ours" });

  assert.strictEqual(st.getUserState("bat:1", "me").status, "watch");
  assert.strictEqual(st.getUserState("bat:1", "me").notes, "mine");
  assert.strictEqual(st.getUserState("bat:1", "me").max_bid, 50000);
  assert.strictEqual(st.getUserState("bat:1", "dad").status, "pass");      // isolated
  assert.strictEqual(st.getUserState("bat:1", "dad").notes, "dad's");
  // shared visible from either profile
  assert.strictEqual(st.resolveUserState("bat:2", "me").status, "bid_plan");
  assert.strictEqual(st.resolveUserState("bat:2", "dad").status, "bid_plan");
  // own overrides shared
  st.setUserState("bat:2", "me", { status: "research" });
  assert.strictEqual(st.resolveUserState("bat:2", "me").status, "research");
  assert.strictEqual(st.resolveUserState("bat:2", "dad").status, "bid_plan");
});

// --- status validation + coercion ----------------------------------------------------------

test("an invalid status is coerced to none, never stored raw", () => {
  const st = US.createUserState(US.memoryBackend(), opts());
  assert.strictEqual(st.setUserState("bat:9", "me", { status: "bogus" }).status, "none");
});

test("a legacy status value written through setUserState is migrated forward", () => {
  const st = US.createUserState(US.memoryBackend(), opts());
  assert.strictEqual(st.setUserState("bat:1", "me", { status: "watching" }).status, "watch");
  assert.strictEqual(st.setUserState("bat:2", "me", { status: "bid_candidate" }).status, "bid_plan");
});

test("a valid-JSON but invalid status is coerced on read (both own + shared)", () => {
  const be = US.memoryBackend({ "bat_user_state_v1": JSON.stringify({
    "me|bat:1": { auction_key: "bat:1", profile_id: "me", status: "PURCHASED_LOL" },
    "shared|bat:2": { auction_key: "bat:2", profile_id: "shared", status: "FUTURE_STATUS" },
  }) });
  const st = US.createUserState(be, opts());
  assert.strictEqual(st.getUserState("bat:1", "me").status, "none");
  assert.strictEqual(st.resolveUserState("bat:1", "me").status, "none");
  assert.strictEqual(st.resolveUserState("bat:2", "me").status, "none");   // shared fallback branch
});

// --- one-time status-enum migration (NON-LOSSY) --------------------------------------------

test("old-enum records migrate to the new enum on construction without losing the user's intent", () => {
  const be = US.memoryBackend({ "bat_user_state_v1": JSON.stringify({
    "me|bat:1": { auction_key: "bat:1", profile_id: "me", status: "watching", notes: "keep" },
    "me|bat:2": { auction_key: "bat:2", profile_id: "me", status: "researching" },
    "dad|bat:3": { auction_key: "bat:3", profile_id: "dad", status: "bid_candidate" },
    "shared|bat:4": { auction_key: "bat:4", profile_id: "shared", status: "passed" },
  }) });
  const st = US.createUserState(be, opts());
  assert.strictEqual(st._migratedStatusCount, 4, "four records had their status rewritten");
  // the records were rewritten in place with the NEW enum, notes preserved
  assert.strictEqual(st.getUserState("bat:1", "me").status, "watch");
  assert.strictEqual(st.getUserState("bat:1", "me").notes, "keep");
  assert.strictEqual(st.getUserState("bat:2", "me").status, "research");
  assert.strictEqual(st.getUserState("bat:3", "dad").status, "bid_plan");
  assert.strictEqual(st.resolveUserState("bat:4", "me").status, "pass");
  // the persisted blob now holds the NEW value, and migration is idempotent on the next load
  assert.ok(/"watch"/.test(be._dump()["bat_user_state_v1"]) && !/"watching"/.test(be._dump()["bat_user_state_v1"]));
  assert.strictEqual(US.createUserState(be, opts())._migratedStatusCount, 0, "idempotent on reload");
});

test("a failed user-state write during status migration does not mark migration done", () => {
  const seed = { "bat_user_state_v1": JSON.stringify({ "me|bat:1": { auction_key: "bat:1", profile_id: "me", status: "watching" } }) };
  const be = flakyBackend(seed, ["bat_user_state_v1"]);   // user-state write fails; meta write OK
  const a = US.createUserState(be, opts());
  assert.strictEqual(a._migratedStatusCount, 0, "migration reports nothing persisted");
  assert.ok(!(JSON.parse(be._dump()["bat_store_meta_v1"] || "{}").migratedStatusEnum), "flag NOT set on write failure");
  // recovery: a fresh load over the same data migrates exactly once
  const healthy = US.memoryBackend(be._dump());
  assert.strictEqual(US.createUserState(healthy, opts())._migratedStatusCount, 1);
  assert.strictEqual(US.createUserState(healthy, opts())._migratedStatusCount, 0, "idempotent after recovery");
});

// --- tags + last_viewed --------------------------------------------------------------------

test("tags are stored, deduped (case-insensitive), and capped; last_viewed is tracked", () => {
  const st = US.createUserState(US.memoryBackend(), opts());
  const rec = st.setUserState("bat:1", "me", { tags: ["Garage Queen", "garage queen", "  driver  ", ""] });
  assert.deepStrictEqual(rec.tags, ["Garage Queen", "driver"], "trimmed + de-duped, blanks dropped");
  // default record carries an empty tags array and a null last_viewed (never undefined)
  const def = st.getUserState("bat:404", "me");
  assert.deepStrictEqual(def.tags, []);
  assert.strictEqual(def.last_viewed, null);
});

test("touchLastViewed updates last_viewed without disturbing status/notes", () => {
  const st = US.createUserState(US.memoryBackend(), opts());
  st.setUserState("bat:1", "me", { status: "watch", notes: "n" });
  const t = st.touchLastViewed("bat:1", "me");
  assert.strictEqual(typeof t.last_viewed, "number");
  assert.strictEqual(t.status, "watch", "viewing does not change the status");
  assert.strictEqual(t.notes, "n");
  // touching a never-seen car records the view but leaves status at none
  const fresh = st.touchLastViewed("bat:2", "me");
  assert.strictEqual(fresh.status, "none");
  assert.strictEqual(typeof fresh.last_viewed, "number");
});

// --- corruption recovery + no-large-records ------------------------------------------------

test("corrupted user-state JSON recovers to none without throwing or erasing the bad blob", () => {
  const be = US.memoryBackend({ "bat_user_state_v1": "also broken]" });
  let st;
  assert.doesNotThrow(() => { st = US.createUserState(be, opts()); });
  assert.strictEqual(st.getUserState("bat:1", "me").status, "none");
  // a subsequent valid write works and the app keeps going
  assert.ok(st.setUserState("bat:1", "me", { status: "watch" }));
  assert.strictEqual(st.getUserState("bat:1", "me").status, "watch");
});

test("a user-state patch can't smuggle large/extraneous fields into storage", () => {
  const st = US.createUserState(US.memoryBackend(), opts());
  const fatCar = { title: "x".repeat(5000), raw_html: "y".repeat(10000) };
  const s = st.setUserState("bat:1", "me", { status: "watch", notes: "ok", car: fatCar, bogus: 123 });
  assert.ok(!("car" in s) && !("bogus" in s), "extraneous user-state keys dropped");
  assert.deepStrictEqual(Object.keys(s).sort(), ["auction_key", "bid_plan", "decision_rationale",
    "inspection_findings", "last_viewed", "max_bid", "max_bid_currency", "notes", "profile_id",
    "questions", "status", "tags", "updated_at"]);
});

// --- Stage 7: bid plan + private fields persist (per auction + profile) ---------------------

test("a bid plan + private fields persist, whitelisted and survive reload", () => {
  const be = US.memoryBackend();
  const a = US.createUserState(be, opts());
  a.setUserState("bat:1", "me", {
    bid_plan: { total_budget: 80000, tax_rate: 0.08, shipping: 1500, fee_rule: "bat-standard",
                junk: { huge: "x".repeat(9000) }, raw_html: "y".repeat(9000) },   // junk must be dropped
    questions: "Service history? Accident-free?", inspection_findings: "Minor oil seep",
    decision_rationale: "Strong comps, clean title",
  });
  const s = US.createUserState(be, opts()).getUserState("bat:1", "me");   // reload over the same backend
  assert.deepStrictEqual(Object.keys(s.bid_plan).sort(), ["fee_rule", "shipping", "tax_rate", "total_budget"]);
  assert.strictEqual(s.bid_plan.total_budget, 80000);
  assert.strictEqual(s.bid_plan.fee_rule, "bat-standard");
  assert.ok(!("junk" in s.bid_plan) && !("raw_html" in s.bid_plan), "no large/extraneous plan keys persisted");
  assert.strictEqual(s.questions, "Service history? Accident-free?");
  assert.strictEqual(s.inspection_findings, "Minor oil seep");
  assert.strictEqual(s.decision_rationale, "Strong comps, clean title");
  // null clears the plan; default record (no plan) reads null, never undefined
  a.setUserState("bat:1", "me", { bid_plan: null });
  assert.strictEqual(a.getUserState("bat:1", "me").bid_plan, null);
  assert.strictEqual(a.getUserState("bat:404", "me").bid_plan, null);
});

// --- mutation returns null when the persist fails (so the UI can warn) ----------------------

test("setUserState returns null when the write fails", () => {
  const stFail = US.createUserState(flakyBackend({}, ["bat_user_state_v1"]), opts());
  assert.strictEqual(stFail.setUserState("bat:1", "me", { status: "watch" }), null);
});
