"""Map timing (begin_at / end_at per map) from PandaScore for every Polymarket CS2 match (pm_events.game_id).

Already fetched finished matches are skipped; re-running is safe.

  python scripts/collect_pandascore.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from pmbot import storage  # noqa: E402
from pmbot.pandascore import PAGE, PandaScore, save_matches  # noqa: E402


def main() -> None:
    load_dotenv(ROOT / ".env")
    conn = storage.connect()
    ids = [row[0] for row in conn.execute(
        """SELECT DISTINCT game_id FROM pm_events WHERE game_id IS NOT NULL AND game_id NOT IN
               (SELECT match_id FROM ps_matches WHERE status IN ('finished', 'canceled', 'missing'))
           ORDER BY game_id""")]
    print(f"matches to fetch: {len(ids)}", flush=True)
    ps = PandaScore()
    try:
        for i in range(0, len(ids), PAGE):
            batch = ids[i:i + PAGE]
            save_matches(conn, batch, ps.matches(batch))
            print(f"{min(i + PAGE, len(ids))}/{len(ids)}", flush=True)
    finally:
        ps.close()
    n = conn.execute("SELECT COUNT(*), SUM(begin_at IS NOT NULL AND end_at IS NOT NULL) FROM ps_games").fetchone()
    print(f"ps_games: {n[0]} maps, {n[1]} with begin/end", flush=True)


if __name__ == "__main__":
    main()
