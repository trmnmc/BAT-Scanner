// Snapshot sanity report (no hardcoded expected totals).
//   node tools/verify_snapshot.js [path-to-auctions.json]
// Reports the counts the spec asks for and exits non-zero if an invariant breaks
// (duplicate chart ids must be 0; the active count must equal the closing-metric count).
const fs = require("fs");
const path = require("path");
const M = require("../web/map-data.js");

const file = process.argv[2] || path.join(__dirname, "..", "data", "auctions.json");
const snap = JSON.parse(fs.readFileSync(file, "utf8"));
const cars = snap.auctions || [];
const now = Date.parse(snap.scraped_at);   // evaluate AT the snapshot's scan time

const active = M.getActiveAuctions(cars, now);
const closingPts = M.buildPoints(cars, "closing", now);
const ids = closingPts.map((p) => p.id);
const dupIds = ids.length - new Set(ids).size;
const noBidLane = closingPts.filter((p) => p.noBid).length;
const withComments = cars.filter((c) => c.engagement && c.engagement.comments != null).length;
const withWatchers = cars.filter((c) => c.engagement && c.engagement.watchers != null).length;
const zeroBid = cars.filter((c) => !(c.bid && c.bid.amount > 0)).length;

const rep = {
  file: path.relative(process.cwd(), file),
  scraped_at: snap.scraped_at,
  total_records: cars.length,
  active_at_scraped_at: active.length,
  closing_metric_points: closingPts.length,
  zero_bid_records: zeroBid,
  zero_bid_in_no_bid_lane: noBidLane,
  records_with_comments: withComments,
  records_with_watchers: withWatchers,
  duplicate_chart_ids: dupIds,
};
console.log(JSON.stringify(rep, null, 2));

const problems = [];
if (dupIds !== 0) problems.push(`duplicate chart ids: ${dupIds} (must be 0)`);
if (active.length !== closingPts.length)
  problems.push(`active (${active.length}) != closing points (${closingPts.length})`);
if (zeroBid !== noBidLane)
  problems.push(`zero-bid records (${zeroBid}) != no-bid lane points (${noBidLane})`);
if (problems.length) {
  console.error("\nFAILED:\n  - " + problems.join("\n  - "));
  process.exit(1);
}
console.log("\nOK: one point per active auction, zero duplicate ids, zero-bids in the no-bid lane.");
