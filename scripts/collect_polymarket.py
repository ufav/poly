"""Collect closed Polymarket CS2 events, markets and minute price history into SQLite.

Re-running is safe: events are upserted, price history is fetched only for markets still 'pending'.

Examples:
  python scripts/collect_polymarket.py --since 2025-09-01
  python scripts/collect_polymarket.py --since 2026-10-01 --until 2026-10-08
  python scripts/collect_polymarket.py --histories-only
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
    args = parser.parse_args()

    since = dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.timezone.utc)
    until = (dt.datetime.fromisoformat(args.until).replace(tzinfo=dt.timezone.utc)
             if args.until else dt.datetime.now(dt.timezone.utc))

    http = HttpClient()
    conn = storage.connect()
    try:
        if not args.histories_only:
            n = 0
            for event in polymarket.iter_closed_events(http, since, until):
                polymarket.save_event(conn, event)
                n += 1
                if n % 200 == 0:
                    conn.commit()
                    print(f"  events {n} (last start {event.get('startTime')})", flush=True)
            conn.commit()
            print(f"events saved: {n}", flush=True)

        done = polymarket.collect_pending_histories(http, conn, args.history_limit)
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
