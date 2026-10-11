"""Fetch roster history (player nationality, since/till) for every team seen in cs_matches.

Run after collect_cybersport.py; teams already fetched are skipped unless --refresh.

  python scripts/collect_cs_teams.py
  python scripts/collect_cs_teams.py --sport dota2
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import cybersport, storage  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--sport", choices=storage.SPORTS, default="cs2")
    args = parser.parse_args()

    http = HttpClient(min_interval=0.3)
    conn = storage.connect(storage.db_path(args.sport))
    seen = "" if args.refresh else "AND id NOT IN (SELECT id FROM cs_teams)"
    team_ids = [row[0] for row in conn.execute(f"""
        SELECT id FROM (SELECT team1_id AS id FROM cs_matches UNION SELECT team2_id FROM cs_matches)
        WHERE id IS NOT NULL {seen}""")]
    print(f"teams to fetch: {len(team_ids)}", flush=True)
    try:
        for i, team_id in enumerate(team_ids, 1):
            try:
                cybersport.save_team(conn, cybersport.fetch_team(http, team_id))
            except Exception as e:  # one broken team page must not stop the run
                print(f"team {team_id}: {e}", flush=True)
            if i % 50 == 0:
                print(f"{i}/{len(team_ids)}", flush=True)
    finally:
        conn.close()
        http.close()
    print("done", flush=True)


if __name__ == "__main__":
    main()
