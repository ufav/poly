"""Collect closed Polymarket events (CS2, Dota 2 or tennis), markets and minute price history into SQLite.

Re-running is safe: events are upserted, price history is fetched only for markets still 'pending'.

Examples:
  python scripts/collect_polymarket.py --since 2025-09-01
  python scripts/collect_polymarket.py --since 2026-10-01 --until 2026-10-08
  python scripts/collect_polymarket.py --histories-only
  python scripts/collect_polymarket.py --sport dota2 --no-histories     # -> data/dota2.sqlite
  python scripts/collect_polymarket.py --sport tennis --since 2025-10-01  # ATP + WTA + ITF singles
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import polymarket, storage  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2025-09-01", help="event creation date from (UTC, YYYY-MM-DD)")
    parser.add_argument("--until", default=None, help="event creation date to (UTC, YYYY-MM-DD), default now")
    parser.add_argument("--histories-only", action="store_true", help="skip event listing, only fetch pending histories")
    parser.add_argument("--history-limit", type=int, default=None)
    parser.add_argument("--sport", choices=storage.ALL_SPORTS, default="cs2",
                        help="game; own database data/<sport>.sqlite")
    parser.add_argument("--no-histories", action="store_true", help="only events and markets")
    parser.add_argument("--history-min-volume", type=float, default=0,
                        help="histories only for markets with volume >= USD (tennis ITF: most are empty)")
    args = parser.parse_args()

    since = dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.timezone.utc)
    until = (dt.datetime.fromisoformat(args.until).replace(tzinfo=dt.timezone.utc)
             if args.until else dt.datetime.now(dt.timezone.utc))

    http = HttpClient()
    conn = storage.connect(storage.db_path(args.sport))
    try:
        if not args.histories_only:
            n = 0
            for series_id in polymarket.SPORT_SERIES[args.sport]:
                for event in polymarket.iter_closed_events(http, since, until, series_id=series_id):
                    polymarket.save_event(conn, event)
                    n += 1
                    if n % 200 == 0:
                        conn.commit()
                        print(f"  events {n} (series {series_id}, last start {event.get('startTime')})", flush=True)
            conn.commit()
            print(f"events saved: {n}", flush=True)

        if not args.no_histories:
            done = polymarket.collect_pending_histories(http, conn, args.history_limit, args.history_min_volume)
            print(f"histories processed: {done}", flush=True)

        stats = conn.execute(
            """SELECT (SELECT COUNT(*) FROM pm_events), (SELECT COUNT(*) FROM pm_markets),
                      (SELECT COUNT(*) FROM pm_markets WHERE history_status='done'),
                      (SELECT COUNT(*) FROM pm_markets WHERE history_status='empty'),
                      (SELECT COUNT(*) FROM pm_prices)"""
        ).fetchone()
        print("db: events={} markets={} histories_done={} histories_empty={} price_points={}".format(*stats))
    finally:
        conn.close()
        http.close()


if __name__ == "__main__":
    main()
