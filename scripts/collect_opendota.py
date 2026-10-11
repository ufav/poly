"""OpenDota: league Dota 2 games (draft, players, gold/xp per minute, objectives, logs) -> data/dota2.sqlite.

1. SQL explorer, 100 games per request: all league games from --since. Its table is complete until ~May 2026, then
   holds only a fraction of games.
2. REST /matches/{id}, one game per request: Steam ids of cybersport maps (cs_games.external_id) still missing —
   run collect_cybersport.py --sport dota2 first.

Re-running is safe: stored games are skipped, except games of the last --refresh-days without per-minute gold
(parsing can lag). Limit without a key: 3000 requests/day — on reaching it the script stops; run again tomorrow.

  python scripts/collect_opendota.py                      # since 2025-06-01 (form/history before Polymarket Dota)
  python scripts/collect_opendota.py --since 2026-10-01
  python scripts/collect_opendota.py --rest-only
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import opendota, storage  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402


def explorer_stage(http: HttpClient, conn, since: dt.datetime, until: dt.datetime, refresh_from: int) -> None:
    print("reference:", opendota.save_reference(http, conn), flush=True)
    ids = opendota.match_ids(http, since, until)
    have = {mid for mid, start, gold in conn.execute(
        "SELECT match_id, start_time, radiant_gold_adv IS NOT NULL FROM od_matches") if gold or start < refresh_from}
    todo = [i for i in ids if i not in have]
    print(f"explorer games {since:%Y-%m-%d}..{until:%Y-%m-%d}: {len(ids)}, to fetch {len(todo)}", flush=True)
    done = 0
    for i in range(0, len(todo), opendota.BATCH):
        done += opendota.save_batch(conn, opendota.fetch_batch(http, todo[i:i + opendota.BATCH]))
        print(f"{min(i + opendota.BATCH, len(todo))}/{len(todo)} saved {done}", flush=True)


def rest_stage(http: HttpClient, conn, refresh_from: int) -> None:
    patches = dict(conn.execute("SELECT id, name FROM od_patches"))
    have = {mid for mid, start, gold in conn.execute(
        "SELECT match_id, start_time, radiant_gold_adv IS NOT NULL FROM od_matches") if gold or start < refresh_from}
    have |= {r[0] for r in conn.execute("SELECT match_id FROM od_missing")}
    ids = sorted({int(r[0]) for r in conn.execute(
        "SELECT external_id FROM cs_games WHERE external_id GLOB '[0-9]*'")} - have, reverse=True)
    print(f"REST: cybersport games missing in OpenDota tables: {len(ids)}", flush=True)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    saved = missing = 0
    for n, mid in enumerate(ids, 1):
        try:
            batch = opendota.fetch_match(http, mid, patches)
        except opendota.ExplorerError as e:
            print(f"stopped at {n - 1}/{len(ids)}: {e}", flush=True)
            break
        if batch is None:
            with conn:
                conn.execute("INSERT OR REPLACE INTO od_missing (match_id, fetched_at) VALUES (?,?)", (mid, now))
            missing += 1
        else:
            saved += opendota.save_batch(conn, batch)
        if n % 100 == 0:
            print(f"{n}/{len(ids)} saved {saved} not in OpenDota {missing}", flush=True)
    print(f"REST saved {saved}, not in OpenDota {missing}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2025-06-01")
    parser.add_argument("--until", default=None, help="exclusive, default now")
    parser.add_argument("--refresh-days", type=int, default=3)
    parser.add_argument("--rest-only", action="store_true", help="skip the explorer stage")
    args = parser.parse_args()

    since = dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.timezone.utc)
    until = (dt.datetime.fromisoformat(args.until).replace(tzinfo=dt.timezone.utc)
             if args.until else dt.datetime.now(dt.timezone.utc))
    refresh_from = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.refresh_days)).timestamp())

    http = HttpClient(min_interval=1.05)
    conn = storage.connect(storage.db_path("dota2"))
    try:
        if not args.rest_only:
            explorer_stage(http, conn, since, until, refresh_from)
        rest_stage(http, conn, refresh_from)
        n = conn.execute("""SELECT COUNT(*), SUM(radiant_gold_adv IS NOT NULL),
                                   (SELECT COUNT(*) FROM od_players), (SELECT COUNT(*) FROM od_picks_bans)
                            FROM od_matches""").fetchone()
        print(f"od_matches {n[0]} (with gold per minute {n[1]}), od_players {n[2]}, od_picks_bans {n[3]}", flush=True)
    finally:
        conn.close()
        http.close()


if __name__ == "__main__":
    main()
