"""Collect CS2 matches, maps and round-by-round data from cybersport.ru into SQLite.

Re-running is safe: finished matches already stored are skipped, except the last --refresh-days
(round stats may appear with a delay); for skipped matches only team ids and tournament location/dates are updated.

Examples:
  python scripts/collect_cybersport.py --since 2025-09-01
  python scripts/collect_cybersport.py --since 2026-10-09 --until 2026-10-10
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import cybersport, storage  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2025-09-01")
    parser.add_argument("--until", default=None, help="inclusive, default today")
    parser.add_argument("--refresh-days", type=int, default=3)
    args = parser.parse_args()

    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until) if args.until else dt.date.today()
    refresh_from = dt.date.today() - dt.timedelta(days=args.refresh_days)

    http = HttpClient(min_interval=0.3)
    conn = storage.connect()
    known = {row[0] for row in conn.execute("SELECT id FROM cs_matches WHERE is_finished = 1")}
    totals = {"matches": 0, "with_rounds": 0, "skipped": 0}
    try:
        day = since
        while day <= until:
            day_stats = {"matches": 0, "with_rounds": 0}
            for match in cybersport.list_matches(http, day):
                if not match["is_finished"]:
                    continue
                if match["id"] in known and day < refresh_from:
                    cybersport.save_meta(conn, match)
                    totals["skipped"] += 1
                    continue
                stats = cybersport.fetch_stats(http, match["id"])
                games = cybersport.fetch_games(http, match["id"]) if stats else []
                cybersport.save_match(conn, match, stats, games)
                day_stats["matches"] += 1
                day_stats["with_rounds"] += int(stats is not None)
            for k, v in day_stats.items():
                totals[k] += v
            print(f"{day}: matches={day_stats['matches']} with_rounds={day_stats['with_rounds']} | total {totals}", flush=True)
            day += dt.timedelta(days=1)
    finally:
        conn.close()
        http.close()


if __name__ == "__main__":
    main()
