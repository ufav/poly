"""Taker trades (Data API) for match and map markets -> pm_trades: what really traded and at what price.

Default scope: events linked to cybersport matches with rounds (the samples of R-8 / R-12) plus all BO3 events
(R-13) with --all. Re-running is safe (markets with trades_status set are skipped).

  python scripts/collect_trades.py [--all] [--limit N] [--sport dota2]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import polymarket, storage  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="all CS2 events, not only those with round data")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--sport", choices=storage.SPORTS, default="cs2", help="dota2: always all events")
    args = ap.parse_args()
    conn = storage.connect(storage.db_path(args.sport))
    scope = "" if args.all or args.sport != "cs2" else (
        "AND e.game_id IN (SELECT external_id FROM cs_matches WHERE stats_status = 'done' AND external_id IS NOT NULL)")
    rows = conn.execute(
        f"""SELECT m.id, m.condition_id FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
            WHERE m.market_type IN ('moneyline', 'child_moneyline') AND m.condition_id IS NOT NULL
              AND m.trades_status IS NULL {scope}
            ORDER BY e.start_time DESC""" + (f" LIMIT {int(args.limit)}" if args.limit else "")).fetchall()
    print(f"markets to fetch: {len(rows)}", flush=True)
    http = HttpClient(min_interval=0.1)
    n_trades = 0
    try:
        for i, (market_id, cond) in enumerate(rows, 1):
            trades = polymarket.fetch_trades(http, cond)
            polymarket.save_trades(conn, market_id, trades)
            n_trades += len(trades or [])
            if i % 200 == 0 or i == len(rows):
                print(f"{i}/{len(rows)} trades {n_trades}", flush=True)
    finally:
        http.close()


if __name__ == "__main__":
    main()
