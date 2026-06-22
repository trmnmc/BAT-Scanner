// Tests for web/search.js — run: node --test web/search.test.js
const test = require("node:test");
const assert = require("node:assert");
const S = require("./search.js");

test("normalizeSearchSpec lowercases/trims, dedupes, drops empties + unknown fields", () => {
  const raw = {
    makes: ["BMW", " bmw ", "", "Audi"],
    models: ["E30"],
    currencies: ["usd", "usd", "eur"],
    requiredTerms: ["Diesel"],
    bogusField: "ignored",
    noReserve: true, dealsOnly: false,
  };
  const out = S.normalizeSearchSpec(raw);
  assert.deepStrictEqual(out.makes, ["bmw", "audi"]);     // trimmed, lowered, deduped
  assert.deepStrictEqual(out.models, ["e30"]);
  assert.deepStrictEqual(out.currencies, ["USD", "EUR"]); // currencies uppercased
  assert.deepStrictEqual(out.requiredTerms, ["diesel"]);
  assert.strictEqual(out.noReserve, true);
  assert.ok(!("dealsOnly" in out), "false boolean is omitted");
  assert.ok(!("bogusField" in out), "unknown field dropped");
});

test("normalizeSearchSpec does not mutate the input", () => {
  const raw = { makes: ["BMW"], priceMax: 35000 };
  const copy = JSON.parse(JSON.stringify(raw));
  S.normalizeSearchSpec(raw);
  assert.deepStrictEqual(raw, copy);
});

test("normalizeSearchSpec clamps years and rejects negative prices", () => {
  assert.strictEqual(S.normalizeSearchSpec({ yearMin: 1700 }).yearMin, 1885);  // clamp low
  assert.strictEqual(S.normalizeSearchSpec({ yearMax: 9999 }).yearMax, 2100);  // clamp high
  assert.ok(!("priceMin" in S.normalizeSearchSpec({ priceMin: -5 })), "negative price dropped");
  assert.ok(!("priceMax" in S.normalizeSearchSpec({ priceMax: "NaN" })), "non-finite dropped");
  assert.ok(!("endingWithinHours" in S.normalizeSearchSpec({ endingWithinHours: -1 })), "non-positive hours dropped");
});

test("normalizeSearchSpec caps array sizes", () => {
  const big = Array.from({ length: 200 }, (_, i) => "term" + i);
  assert.ok(S.normalizeSearchSpec({ requiredTerms: big }).requiredTerms.length <= 40);
});

test("normalizeSearchSpec termGroups must be arrays of short string arrays", () => {
  const out = S.normalizeSearchSpec({ termGroups: [["Wagon", "estate"], "not-an-array", [], ["manual"]] });
  assert.deepStrictEqual(out.termGroups, [["wagon", "estate"], ["manual"]]); // non-array + empty dropped, lowered
});

test("validateSearchSpec accepts good specs, rejects malformed", () => {
  assert.strictEqual(S.validateSearchSpec({ makes: ["bmw"], priceMax: 35000 }).valid, true);
  assert.strictEqual(S.validateSearchSpec({}).valid, true);
  assert.strictEqual(S.validateSearchSpec(null).valid, false);
  assert.strictEqual(S.validateSearchSpec({ priceMax: -1 }).valid, false);
  assert.strictEqual(S.validateSearchSpec({ yearMin: "soon" }).valid, false);
  assert.strictEqual(S.validateSearchSpec({ makes: "bmw" }).valid, false);          // not an array
  assert.strictEqual(S.validateSearchSpec({ termGroups: [["a"], "b"] }).valid, false); // not arrays-of-arrays
  // unknown fields are allowed (ignored by normalizer)
  assert.strictEqual(S.validateSearchSpec({ makes: ["bmw"], wat: 1 }).valid, true);
});

test("basicKeywordSpec turns a query into AND requiredTerms", () => {
  assert.deepStrictEqual(S.basicKeywordSpec("manual wagon").requiredTerms, ["manual", "wagon"]);
  assert.deepStrictEqual(S.basicKeywordSpec("  E30   325is "), { requiredTerms: ["e30", "325is"] });
  assert.deepStrictEqual(S.basicKeywordSpec(""), {});
});

test("buildSearchableText delegates to the filter engine", () => {
  const car = { title: "1987 BMW E30 Wagon", make: { name: "BMW", slug: "bmw" }, models: [{ slug: "e30" }], taxonomy_paths: ["bmw/e30"] };
  const t = S.buildSearchableText(car);
  assert.ok(t.includes("bmw") && t.includes("e30") && t.includes("wagon"));
});

test("formatSearchSummary describes constraints", () => {
  const s = S.formatSearchSummary({ makes: ["bmw"], priceMax: 35000, termGroups: [["wagon", "estate"]] });
  assert.ok(s.includes("bmw"));
  assert.ok(s.includes("35,000"));
  assert.ok(s.includes("wagon/estate"));
  assert.strictEqual(S.formatSearchSummary({}), "Showing the whole board");
});

test("specConstraints yields removable chips, one per constraint + per term group", () => {
  const chips = S.specConstraints({ makes: ["bmw"], priceMax: 35000, termGroups: [["wagon"], ["manual"]] });
  const keys = chips.map((c) => c.key);
  assert.ok(keys.includes("makes"));
  assert.ok(keys.includes("priceMax"));
  assert.ok(keys.includes("termGroups:0"));
  assert.ok(keys.includes("termGroups:1"));
});

test("buildCatalog derives unique makes + currencies from cars", () => {
  const cars = [
    { make: { name: "BMW", slug: "bmw" }, bid: { currency: "USD" } },
    { make: { name: "BMW", slug: "bmw" }, bid: { currency: "EUR" } },
    { make: { name: "Audi", slug: "audi" }, bid: { currency: "USD" } },
  ];
  const cat = S.buildCatalog(cars);
  assert.deepStrictEqual(cat.makes.map((m) => m.slug), ["audi", "bmw"]);
  assert.deepStrictEqual(cat.currencies, ["EUR", "USD"]);
});

// --- interpretSearch (dependency-injected fetch) ---

const okRes = (obj) => ({ ok: true, json: () => Promise.resolve(obj) });

test("interpretSearch with no endpoint -> keyword spec", async () => {
  const r = await S.interpretSearch("manual wagon", {});
  assert.strictEqual(r.source, "keyword");
  assert.deepStrictEqual(r.spec.requiredTerms, ["manual", "wagon"]);
});

test("interpretSearch success -> normalized llm spec", async () => {
  const fetchImpl = async () => okRes({
    version: 1, summary: "Manual German wagons under $35,000",
    spec: { makes: ["AUDI", "bmw"], priceMax: 35000, termGroups: [["wagon", "avant"]], bogus: 1 },
  });
  const r = await S.interpretSearch("manual german wagon under 35k", { endpoint: "https://x/api", fetchImpl });
  assert.strictEqual(r.source, "llm");
  assert.deepStrictEqual(r.spec.makes, ["audi", "bmw"]); // normalized
  assert.strictEqual(r.spec.priceMax, 35000);
  assert.ok(!("bogus" in r.spec));
  assert.strictEqual(r.summary, "Manual German wagons under $35,000");
});

test("interpretSearch falls back to keyword on HTTP error", async () => {
  const fetchImpl = async () => ({ ok: false, json: () => Promise.resolve({}) });
  const r = await S.interpretSearch("e30 wagon", { endpoint: "https://x/api", fetchImpl });
  assert.strictEqual(r.source, "keyword");
  assert.ok(r.error);
  assert.deepStrictEqual(r.spec.requiredTerms, ["e30", "wagon"]);
});

test("interpretSearch falls back on thrown/network error", async () => {
  const fetchImpl = async () => { throw new Error("network down"); };
  const r = await S.interpretSearch("porsche 911", { endpoint: "https://x/api", fetchImpl });
  assert.strictEqual(r.source, "keyword");
  assert.ok(r.error);
});

test("interpretSearch falls back when the spec is invalid or empty", async () => {
  const invalid = async () => okRes({ version: 1, spec: { priceMax: -5 } });   // invalid
  let r = await S.interpretSearch("cheap", { endpoint: "https://x/api", fetchImpl: invalid });
  assert.strictEqual(r.source, "keyword");

  const empty = async () => okRes({ version: 1, spec: { onlyBogusFields: 1 } }); // normalizes to {}
  r = await S.interpretSearch("anything", { endpoint: "https://x/api", fetchImpl: empty });
  assert.strictEqual(r.source, "keyword");
});

test("interpretSearch never retains a partially-invalid spec (no leak)", async () => {
  // valid spec wrapper but contains a bad number alongside good fields -> whole thing rejected
  const fetchImpl = async () => okRes({ version: 1, spec: { makes: ["bmw"], priceMax: "lots" } });
  const r = await S.interpretSearch("bmw", { endpoint: "https://x/api", fetchImpl });
  assert.strictEqual(r.source, "keyword", "invalid number invalidates the whole LLM spec");
  assert.ok(!r.spec.priceMax);
});
