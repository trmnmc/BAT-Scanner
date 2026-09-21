// Static contract between web/index.html's markup and its inline script: every element the
// script looks up by id must exist in the markup, and the layout hooks the CSS/JS rely on
// (stage/rail/chart nesting, the view toggle, the ticker) must keep their ids. This guards a
// restructure of the page shell (CSS + markup) against silently breaking the map's behavior,
// without a browser. Node's test runner, no dependencies.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const bodyStart = html.indexOf("<body>");
const scriptStart = html.indexOf("<script>", bodyStart);
assert.ok(bodyStart > 0 && scriptStart > bodyStart, "index.html has a <body> followed by the inline <script>");
const markup = html.slice(bodyStart, scriptStart);
const script = html.slice(scriptStart);

function idsInMarkup(src) {
  const ids = new Set();
  for (const m of src.matchAll(/\bid="([^"]+)"/g)) ids.add(m[1]);
  return ids;
}

test("every id the inline script looks up exists exactly once in the markup", () => {
  const have = idsInMarkup(markup);
  const wanted = new Set();
  for (const m of script.matchAll(/getElementById\(["']([^"']+)["']\)/g)) wanted.add(m[1]);
  // ids the script creates at runtime (rendered into innerHTML) are not markup ids
  const runtime = new Set(["bid-calc"]);
  const missing = [...wanted].filter((id) => !have.has(id) && !runtime.has(id));
  assert.deepEqual(missing, [], `ids referenced by the script but absent from the markup: ${missing.join(", ")}`);
  for (const id of have) {
    const n = markup.split(`id="${id}"`).length - 1;
    assert.equal(n, 1, `id="${id}" must appear once, found ${n}`);
  }
  assert.ok(wanted.size >= 40, `sanity: the script references many ids (${wanted.size})`);
});

test("layout hooks keep their nesting: chart + legends inside #mapcol, #mapcol + #rail inside #stage", () => {
  const stage = markup.indexOf('id="stage"'), stageEnd = markup.indexOf('id="listpanel"');
  assert.ok(stage > 0 && stageEnd > stage);
  const inStage = markup.slice(stage, stageEnd);
  for (const id of ["mapcol", "chart", "deallegend", "badgelegend", "rail"]) {
    assert.ok(inStage.includes(`id="${id}"`), `#${id} lives inside #stage`);
  }
  // the map/list toggle, the search form and the ticker are still present and hidden-able
  assert.match(markup, /<button id="view-map" class="active"/);
  assert.match(markup, /<button id="view-list"/);
  assert.match(markup, /<form class="search" id="searchform"/);
  assert.match(markup, /<div id="ticker" hidden>/);
  assert.match(markup, /<div id="listpanel" hidden>/);
  assert.match(markup, /<div id="card" role="dialog"/);
});

test("the metric select offers the six map metrics with the closing runway as default", () => {
  const sel = markup.match(/<select id="ymetric">([\s\S]*?)<\/select>/);
  assert.ok(sel, "#ymetric select present");
  const values = [...sel[1].matchAll(/value="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(values, ["runway", "comments", "closing", "watchers", "deal", "marketcontext"]);
  assert.match(sel[1], /value="runway" selected/);
});

test("mobile and reduced-motion rules survive in the stylesheet", () => {
  const style = html.slice(html.indexOf("<style>"), html.indexOf("</style>"));
  assert.match(style, /@media \(max-width: 480px\)/);
  assert.match(style, /@media \(prefers-reduced-motion: reduce\)/);
  const mobile = style.slice(style.indexOf("@media (max-width: 480px)"));
  assert.match(mobile, /min-height: 44px/, "phone touch targets reach 44px");
  assert.match(mobile, /#ticker \.ticker-track \{ animation: none; \}/, "phone ticker scrolls instead of marquee");
  assert.match(mobile, /#card \{ top: auto; bottom: 0;/, "phone Auction Brief is a bottom sheet");
  const rm = style.slice(style.indexOf("@media (prefers-reduced-motion: reduce)"));
  assert.match(rm, /\.ticker-track \{ animation: none; \}/);
});

test("no forbidden valuation language in the page copy", () => {
  const text = markup.replace(/<[^>]+>/g, " ");
  for (const w of ["undervalued", "overpriced", "guaranteed bargain", "don't buy"]) {
    assert.ok(!text.toLowerCase().includes(w), `page copy must not say "${w}"`);
  }
});
