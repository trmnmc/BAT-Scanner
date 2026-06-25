#!/usr/bin/env python3
"""History report — a read-only summary of the recorded auction-change log (Stage 9 task 10).

    python tools/history_report.py [--history data/history.json] [--json]
                                   [--movement KEY] [--metric bid|comments|watchers]

Surfaces:
  - total events
  - file size
  - auctions tracked (distinct auction_key)
  - event counts by type
  - oldest and newest event (observed_at)

With --movement bat:<id> it also prints the "daily movement" of a metric for one auction
(sparse daily observations, not a live rate). Read-only and offline: it never fetches, never
writes, and treats a missing/empty history as a normal empty log.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import history  # noqa: E402


def _human_bytes(n):
    if n is None:
        return "—"
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} {u}"
        f /= 1024
    return f"{n} B"


def _fmt_text(summary, movement=None):
    out = ["History report", "=" * 48,
           f"File size:         {_human_bytes(summary['file_bytes'])}",
           f"Total events:      {summary['total_events']}",
           f"Auctions tracked:  {summary['auctions_tracked']}",
           f"Oldest event:      {summary['oldest_event'] or '—'}",
           f"Newest event:      {summary['newest_event'] or '—'}",
           "", "Event counts by type:"]
    counts = summary["event_counts_by_type"]
    if counts:
        for etype, n in counts.items():
            out.append(f"   {etype:<24} {n}")
    else:
        out.append("   (none yet — history is empty)")
    if movement is not None:
        out.append("")
        if movement["result"] is None:
            out.append(f"Daily movement [{movement['metric']}] for {movement['key']}: "
                       "unavailable (need at least two valid observations).")
        else:
            r = movement["result"]
            out.append(f"Daily movement [{r['metric']}] for {movement['key']}:")
            out.append(f"   {r['first_value']} -> {r['last_value']} "
                       f"({r['change']:+}) over {r['window_days']} day(s) "
                       f"= {r['per_day']:+}/day  ({r['observations']} observations)")
    return "\n".join(out)


def main(argv=None) -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = argparse.ArgumentParser(description="Auction history audit (read-only).")
    p.add_argument("--history", default=os.path.join(root, "data", "history.json"))
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p.add_argument("--movement", default=None, help="auction_key (e.g. bat:123) to show daily movement for")
    p.add_argument("--metric", default="bid", choices=["bid", "comments", "watchers"])
    args = p.parse_args(argv)

    hist = history.load_history(args.history)
    summary = history.summarize(hist, path=args.history)

    movement = None
    if args.movement:
        movement = {"key": args.movement, "metric": args.metric,
                    "result": history.daily_movement(hist, args.movement, args.metric)}

    if args.json:
        payload = dict(summary)
        if movement is not None:
            payload["movement"] = movement
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(_fmt_text(summary, movement))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
