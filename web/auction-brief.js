// web/auction-brief.js — pure Auction Brief builder, shared by the browser and Node tests.
//
// Turns a NORMALIZED auction object (BATAuctionModel) into a structured brief: sections, rows,
// explicit empty-states, deterministic verdict/tracking phrases (from the APPROVED lists only),
// and non-fabricating placeholders. It renders NO HTML and computes NO scores — every value is a
// fact already present on the record, or an honest "not yet" state. Missing fields are dropped,
// never shown as undefined/NaN/empty/false-zero. Same IIFE + CommonJS pattern as the other modules.
(function (global) {
  "use strict";

  // The ONLY phrases the panel may show (Stage 4 approved lists). Selection is deterministic.
  var VERDICTS = [
    "Worth watching", "Interesting, but likely expensive", "Potentially below expected range",
    "Strong car, weak value", "Too little data yet", "Looks like a bidding-war candidate",
  ];
  var TRACKINGS = [
    "Trading below expected range", "Tracking near expected range", "Tracking above expected range",
    "Too early to estimate", "Low confidence estimate", "High-interest auction",
    "Potential opportunity", "Watch closely",
  ];
  // Future-computed slots — shown as labels with NO value (must not fabricate).
  var PLACEHOLDERS = {
    range: "Estimated final range", bidVelocity: "Bid velocity", commentVelocity: "Comment velocity",
    oppScore: "Opportunity Score", aiVerdict: "AI verdict",
  };
  var STATE = {
    tooEarly: "Too early to estimate", lowConf: "Low confidence estimate",
    noActivity: "Activity not yet observed", noHistory: "History not yet available",
  };

  var TRUSTED_BASES = { "make-model-y3": 1, "make-model-y7": 1 };
  var BASIS_LABEL = { "make-model-y3": "same model ±3y", "make-model-y7": "same model ±7y" };
  var CURRENCY_SYMBOLS = { USD: "$", CAD: "$", AUD: "$", EUR: "€", GBP: "£" };
  var GOOD_CONDITION = { "numbers-matching": 1, "original-paint": 1, "survivor": 1, "matching-numbers": 1 };

  var ENDING_SOON_MS = 24 * 3600 * 1000;
  var BIDDING_WAR_WATCHERS = 500;
  var HIGH_INTEREST_WATCHERS = 300;
  var UNDER_BAND = 0.10;   // deal_pct at/above => "below expected range"
  var OVER_BAND = -0.05;   // deal_pct at/below => "above expected range"

  // ---- safe primitives: a real number (incl 0) survives; null/NaN/garbage -> null. Never "undefined". ----
  function _num(v) { return (typeof v === "number" && isFinite(v)) ? v : null; }
  function _str(v) { return (typeof v === "string" && v.trim() !== "") ? v : null; }
  function _intLabel(v) { var n = _num(v); return n == null ? null : Math.round(n).toLocaleString(); }
  function money(amount, cur) {
    var a = _num(amount); if (a == null) return null;
    var sym = CURRENCY_SYMBOLS[cur] || "";
    return sym + Math.round(a).toLocaleString() + (sym ? "" : (cur ? " " + cur : ""));
  }

  function _value(norm) { return (norm && norm.value && typeof norm.value === "object") ? norm.value : null; }
  function _engagement(norm, k) {
    var e = norm && norm.engagement;
    return (e && _num(e[k]) != null) ? _num(e[k]) : null;
  }
  function isHistorical(norm) {
    return !!(norm && (norm.historical_status === "sold" || norm.historical_status === "historical"));
  }
  function noReserve(norm) { return !!(norm && norm.flags && norm.flags.no_reserve); }
  // An ambiguous identity must not receive a confident valuation (CLAUDE.md invariant).
  function ambiguousIdentity(norm) { return !!(norm && norm.vehicle_identity && norm.vehicle_identity.ambiguous); }
  function hasTrustedEstimate(norm) {
    var v = _value(norm);
    return !!(v && TRUSTED_BASES[v.basis] && _num(v.fair_value) > 0 && !ambiguousIdentity(norm));
  }
  // A discount may be QUOTED only on a trusted basis, a no-reserve auction, and an unambiguous
  // identity. Otherwise we do not state a confident "X% under/over".
  function quotableDealPct(norm) {
    if (!hasTrustedEstimate(norm) || !noReserve(norm)) return null;
    return _num(_value(norm).deal_pct);
  }
  // is_deal may likewise only drive a confident signal when the estimate is trustworthy + no-reserve
  // (+ unambiguous, via hasTrustedEstimate). A raw is_deal on a reserve/untrusted/ambiguous record
  // is NOT a quotable bargain.
  function quotableIsDeal(norm) {
    return hasTrustedEstimate(norm) && noReserve(norm) && !!(_value(norm) && _value(norm).is_deal);
  }

  function estimateState(norm) {
    var v = _value(norm);
    if (!v || !(_num(v.fair_value) > 0) || !(_num(v.n_comps) > 0)) return STATE.tooEarly;
    if (!TRUSTED_BASES[v.basis] || !noReserve(norm) || ambiguousIdentity(norm)) return STATE.lowConf;
    return null;
  }
  function activityState(norm) {
    return (_engagement(norm, "comments") == null && _engagement(norm, "watchers") == null)
      ? STATE.noActivity : null;
  }
  function historyState(norm) {
    var v = _value(norm);
    return (v && _num(v.n_comps) > 0) ? null : STATE.noHistory;
  }

  function _endsInMs(norm, now) {
    var t = (norm && norm.ends_at) ? Date.parse(norm.ends_at) : NaN;
    return isNaN(t) ? null : (t - now);
  }
  function _condSplit(norm) {
    var d = norm && norm.details;
    var cond = (d && Array.isArray(d.condition)) ? d.condition : [];
    var good = [], bad = [];
    cond.forEach(function (c) { if (typeof c === "string") (GOOD_CONDITION[c] ? good : bad).push(c); });
    return { good: good, bad: bad };
  }
  function _milesLabel(norm) {
    var d = norm && norm.details; if (!d) return null;
    if (_num(d.miles) == null) return d.tmu ? "TMU" : null;
    var m = d.miles >= 10000 ? Math.round(d.miles / 1000) + "k mi" : Math.round(d.miles).toLocaleString() + " mi";
    return d.tmu ? m + " · TMU" : m;
  }
  function _strongCar(norm) {
    var cs = _condSplit(norm);
    return cs.good.length > 0 && cs.bad.length === 0;
  }
  function _soldMs(norm) {
    var ts = norm && norm.sold_ts;
    if (typeof ts !== "number" || !isFinite(ts) || ts <= 0) return null;
    return ts < 1e12 ? ts * 1000 : ts;
  }

  // ---- deterministic phrase selection (APPROVED lists only) ----
  function verdictPhrase(norm, now) {
    now = _num(now) != null ? now : 0;
    var v = _value(norm), dp = quotableDealPct(norm), isDeal = quotableIsDeal(norm);
    var watchers = _engagement(norm, "watchers"), comments = _engagement(norm, "comments");
    var hasComps = !!(v && _num(v.n_comps) > 0), hasActivity = (watchers != null || comments != null);
    var ein = _endsInMs(norm, now), endingSoon = (ein != null && ein > 0 && ein <= ENDING_SOON_MS);
    if (!hasComps && !hasActivity) return "Too little data yet";
    if (isDeal || (dp != null && dp >= UNDER_BAND)) return "Potentially below expected range";
    if (watchers != null && watchers >= BIDDING_WAR_WATCHERS && endingSoon) return "Looks like a bidding-war candidate";
    if (_strongCar(norm) && dp != null && dp < 0) return "Strong car, weak value";
    if (dp != null && dp <= OVER_BAND) return "Interesting, but likely expensive";
    return "Worth watching";
  }
  function trackingPhrase(norm) {
    var es = estimateState(norm);
    if (es === STATE.tooEarly) return "Too early to estimate";
    if (es === STATE.lowConf) return "Low confidence estimate";
    var dp = quotableDealPct(norm);
    if (dp == null) return "Too early to estimate";
    if (dp >= 0.05) return "Trading below expected range";
    if (dp <= -0.05) return "Tracking above expected range";
    return "Tracking near expected range";
  }
  function interestPhrase(norm) {
    var watchers = _engagement(norm, "watchers"), comments = _engagement(norm, "comments");
    if (quotableIsDeal(norm)) return "Potential opportunity";
    if (watchers != null && watchers >= HIGH_INTEREST_WATCHERS) return "High-interest auction";
    if (watchers != null || comments != null) return "Watch closely";
    return null;
  }

  function _row(rows, label, value) {
    if (value != null && value !== "") rows.push({ label: label, value: String(value) });
  }
  function _section(id, title, rows, state, placeholders) {
    return { id: id, title: title, rows: rows || [], state: state || null, placeholders: placeholders || [] };
  }

  function _identity(norm) {
    var vi = (norm && norm.vehicle_identity) || {};
    var year = _num(vi.year) != null ? _num(vi.year) : _num(norm && norm.year);
    var makeName = (vi.make && _str(vi.make.name)) || (norm && norm.make && _str(norm.make.name)) || null;
    var modelName = (vi.model && _str(vi.model.name))
      || (norm && Array.isArray(norm.models) && norm.models[0] && _str(norm.models[0].name))
      || _str(norm && norm.model) || null;
    return { vi: vi, year: year, makeName: makeName, modelName: modelName };
  }

  // A historical comp gets a clearly-historical, SIMPLIFIED panel: just the sale facts.
  function _historicalBrief(norm, id) {
    var rows = [];
    _row(rows, "Sold price", money(norm.price, "USD"));
    _row(rows, "Year", id.year != null ? String(id.year) : null);
    _row(rows, "Make", id.makeName);
    _row(rows, "Model", id.modelName);
    var soldMs = _soldMs(norm);
    return {
      historical: true,
      title: _str(norm.title) || "Past sale",
      subtitle: [id.year, id.makeName, id.modelName].filter(function (x) { return x != null; }).join(" ") || null,
      deepLink: (typeof norm.listing_url === "string" && /^https?:\/\//i.test(norm.listing_url)) ? norm.listing_url : null,
      header: { kind: "historical", soldText: money(norm.price, "USD"), soldAtMs: soldMs, label: "Past sale" },
      verdict: null, tracking: null, interest: null,
      sections: [_section("sale", "Past sale", rows, rows.length ? null : "Sale details unavailable", [])],
    };
  }

  // Build the full brief from a normalized auction object.
  //   opts: { nowMs, freshnessLabel }  (badges are rendered by the caller from the click payload)
  function buildBrief(norm, opts) {
    opts = opts || {};
    norm = norm || {};
    var now = _num(opts.nowMs) != null ? opts.nowMs : 0;
    var id = _identity(norm);

    if (isHistorical(norm)) return _historicalBrief(norm, id);

    var V = _value(norm);
    var cur = norm.bid && norm.bid.currency;
    var dpQ = quotableDealPct(norm);
    var dpText = dpQ != null
      ? (dpQ >= 0 ? Math.round(dpQ * 100) + "% under comps" : Math.round(-dpQ * 100) + "% over comps")
      : null;
    var verdict = verdictPhrase(norm, now), tracking = trackingPhrase(norm), interest = interestPhrase(norm);
    var noBid = !(norm.bid && _num(norm.bid.amount) > 0);
    var endsT = (norm.ends_at ? Date.parse(norm.ends_at) : NaN);

    var header = {
      kind: "live",
      bidText: noBid ? "No bid yet" : money(norm.bid.amount, cur),
      bidNote: _str(opts.freshnessLabel) ? ("bid at last scan · updated " + opts.freshnessLabel) : "bid at last scan",
      endsAtMs: isNaN(endsT) ? null : endsT,
      reserveText: noReserve(norm) ? "No reserve" : "Reserve",
      deepLink: (typeof norm.listing_url === "string" && /^https?:\/\//i.test(norm.listing_url)) ? norm.listing_url : null,
    };

    var sections = [];

    // 1 — Decision summary (deterministic approved phrases + AI-verdict placeholder)
    var sum = [];
    _row(sum, "Verdict", verdict);
    _row(sum, "Status", tracking);
    _row(sum, "Signal", interest);
    sections.push(_section("summary", "Decision summary", sum, null, [PLACEHOLDERS.aiVerdict]));

    // 2 — Estimate
    var es = estimateState(norm), est = [];
    if (!es) {
      _row(est, "Comp median", money(V.fair_value, cur));
      _row(est, "Vs comps", dpText);
    }
    sections.push(_section("estimate", "Estimate", est, es, [PLACEHOLDERS.range]));

    // 3 — Opportunity components (factors only; the Score itself is a placeholder)
    var opp = [];
    _row(opp, "Discount vs comps", dpText);
    _row(opp, "Watchers", _intLabel(_engagement(norm, "watchers")));
    _row(opp, "Comments", _intLabel(_engagement(norm, "comments")));
    var csOpp = _condSplit(norm);
    if (csOpp.good.length) _row(opp, "Positive condition", csOpp.good.join(", "));
    sections.push(_section("opportunity", "Opportunity components", opp, null, [PLACEHOLDERS.oppScore]));

    // 4 — Comparable sales
    var hs = historyState(norm), comps = [];
    if (!hs) {
      var basisTxt = BASIS_LABEL[V.basis] || _str(V.basis);
      _row(comps, "Comps used", _intLabel(V.n_comps) + (basisTxt ? " (" + basisTxt + ")" : ""));
      _row(comps, "Median", money(V.fair_value, cur));
      if (_num(V.appreciation_pct) != null) {
        _row(comps, "Recent trend", (V.appreciation_pct >= 0 ? "+" : "") + Math.round(V.appreciation_pct * 100) + "%");
      }
    }
    sections.push(_section("comps", "Comparable sales", comps, hs, []));

    // 5 — Auction activity
    var as = activityState(norm), act = [];
    if (!as) {
      _row(act, "Comments", _intLabel(_engagement(norm, "comments")));
      _row(act, "Watchers", _intLabel(_engagement(norm, "watchers")));
    }
    if (!noBid) _row(act, "Current bid", header.bidText);
    sections.push(_section("activity", "Auction activity", act, as, [PLACEHOLDERS.bidVelocity, PLACEHOLDERS.commentVelocity]));

    // 6 — Vehicle / spec facts
    var facts = [];
    _row(facts, "Year", id.year != null ? String(id.year) : null);
    _row(facts, "Make", id.makeName);
    _row(facts, "Model", id.modelName);
    _row(facts, "Trim", _str(id.vi.trim));
    _row(facts, "VIN", _str(id.vi.vin));
    _row(facts, "Mileage", _milesLabel(norm));
    var csFacts = _condSplit(norm);
    if (csFacts.good.length) _row(facts, "Condition (good)", csFacts.good.join(", "));
    if (csFacts.bad.length) _row(facts, "Condition flags", csFacts.bad.join(", "));
    sections.push(_section("facts", "Vehicle / spec facts", facts, facts.length ? null : "Not available", []));

    // 7 — Risks & missing information
    var risks = [];
    if (!noReserve(norm)) risks.push("Reserve auction — current bid may be below reserve, not a discount");
    if (norm.details && norm.details.tmu) risks.push("True mileage unknown (TMU)");
    if (id.vi.ambiguous) risks.push("Vehicle identity not fully confirmed");
    if (csFacts.bad.length) risks.push("Condition flags: " + csFacts.bad.join(", "));
    if (historyState(norm)) risks.push("No trusted comparable sales yet");
    if (activityState(norm)) risks.push("Engagement not yet scanned");
    var riskRows = risks.map(function (r) { return { label: "•", value: r }; });
    sections.push(_section("risks", "Risks & missing information", riskRows, riskRows.length ? null : "None flagged", []));

    // 8 — Seller notes (not captured in the snapshot)
    sections.push(_section("seller", "Seller notes", [], "Not available", []));

    // 9 — User plan (no persistence yet)
    sections.push(_section("plan", "Your plan", [], "No plan saved yet", []));

    return {
      historical: false,
      title: _str(norm.title) || "Untitled listing",
      subtitle: [id.year, id.makeName, id.modelName].filter(function (x) { return x != null; }).join(" ") || null,
      deepLink: header.deepLink,
      header: header,
      verdict: verdict, tracking: tracking, interest: interest,
      sections: sections,
    };
  }

  var api = {
    VERDICTS: VERDICTS, TRACKINGS: TRACKINGS, PLACEHOLDERS: PLACEHOLDERS, STATE: STATE,
    isHistorical: isHistorical,
    estimateState: estimateState, activityState: activityState, historyState: historyState,
    quotableDealPct: quotableDealPct,
    verdictPhrase: verdictPhrase, trackingPhrase: trackingPhrase, interestPhrase: interestPhrase,
    buildBrief: buildBrief,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;  // node tests
  global.BATAuctionBrief = api;                                              // browser
})(typeof window !== "undefined" ? window : globalThis);
