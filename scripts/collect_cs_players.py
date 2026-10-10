"""Backfill per-map player stats (kills, deaths, ADR, KAST, rating) for matches already stored with rounds.

New matches get player stats in collect_cybersport.py; this is for the history. Re-running is safe.

  python scripts/collect_cs_players.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import cybersport, storage  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402


def main() -> None:
    conn = storage.connect()
    ids = [row[0] for row in conn.execute(
        """SELECT id FROM cs_matches WHERE stats_status = 'done'
           AND id NOT IN (SELECT match_id FROM cs_player_stats_status) ORDER BY date""")]
    print(f"matches to fetch: {len(ids)}", flush=True)
    http = HttpClient(min_interval=0.3)
    total = 0
    try:
        for i, match_id in enumerate(ids, 1):
            total += cybersport.save_player_stats(conn, match_id, cybersport.fetch_stats(http, match_id))
            if i % 50 == 0 or i == len(ids):
                print(f"{i}/{len(ids)} player-map rows {total}", flush=True)
    finally:
        http.close()
        conn.close()


if __name__ == "__main__":
    main()
