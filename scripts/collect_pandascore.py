"""PandaScore for every Polymarket match (pm_events.game_id): map timing, schedule, tournament (tier, LAN/online,
country, region, prize pool), teams (location); then team rosters (nationality, role).

Already fetched finished Polymarket matches are skipped; re-running is safe. --since also loads all past matches of
the game from that date (not only Polymarket ones; ~100 matches per request) — form and schedule of teams.

  python scripts/collect_pandascore.py
  python scripts/collect_pandascore.py --sport dota2 --since 2025-06-01
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from pmbot import storage  # noqa: E402
from pmbot.pandascore import GAMES, PAGE, PandaScore, save_matches, save_teams  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", choices=storage.SPORTS, default="cs2")
    parser.add_argument("--since", default=None, help="also all past matches from this date (YYYY-MM-DD)")
    parser.add_argument("--no-teams", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    conn = storage.connect(storage.db_path(args.sport))
    ps = PandaScore(game=GAMES[args.sport])
    try:
        ids = [row[0] for row in conn.execute(
            """SELECT DISTINCT game_id FROM pm_events WHERE game_id IS NOT NULL AND (game_id NOT IN
                   (SELECT match_id FROM ps_matches WHERE status IN ('finished', 'canceled', 'missing'))
                   OR game_id NOT IN (SELECT match_id FROM ps_match_info UNION
                                      SELECT match_id FROM ps_matches WHERE status = 'missing'))
               ORDER BY game_id""")]
        print(f"Polymarket matches to fetch: {len(ids)}", flush=True)
        for i in range(0, len(ids), PAGE):
            batch = ids[i:i + PAGE]
            save_matches(conn, batch, ps.matches(batch))
            print(f"{min(i + PAGE, len(ids))}/{len(ids)}", flush=True)

        if args.since:
            since = dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.timezone.utc)
            until, page, n = dt.datetime.now(dt.timezone.utc), 1, 0
            while True:
                rows = ps.past_matches(since, until, page)
                save_matches(conn, [m["id"] for m in rows], rows)
                n += len(rows)
                if page % 10 == 0 or len(rows) < PAGE:
                    print(f"past matches from {since:%Y-%m-%d}: {n} (last {rows[-1]['begin_at'] if rows else '-'})",
                          flush=True)
                if len(rows) < PAGE:
                    break
                page += 1

        if not args.no_teams:
            team_ids = [r[0] for r in conn.execute("SELECT id FROM ps_teams WHERE fetched_at IS NULL ORDER BY id")]
            print(f"teams to fetch: {len(team_ids)}", flush=True)
            for i in range(0, len(team_ids), PAGE):
                save_teams(conn, ps.teams(team_ids[i:i + PAGE]))
    finally:
        ps.close()
    n = conn.execute("""SELECT (SELECT COUNT(*) FROM ps_match_info), (SELECT COUNT(*) FROM ps_games),
                               (SELECT SUM(begin_at IS NOT NULL AND end_at IS NOT NULL) FROM ps_games),
                               (SELECT COUNT(*) FROM ps_tournaments), (SELECT COUNT(*) FROM ps_teams),
                               (SELECT COUNT(*) FROM ps_players)""").fetchone()
    print("ps_match_info {} | ps_games {} ({} with begin/end) | tournaments {} | teams {} | players {}".format(*n),
          flush=True)
    conn.close()


if __name__ == "__main__":
    main()
