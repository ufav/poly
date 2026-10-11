"""Link Dota 2 maps: Polymarket event + map number <-> OpenDota game <-> cybersport match (pmbot/dota_join.py).

Run after the collectors (collect_polymarket / collect_pandascore / collect_cybersport / collect_opendota --sport dota2);
rebuilds dota_map_links every time.

  python scripts/link_dota.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import dota_join, storage  # noqa: E402


def main() -> None:
    conn = storage.connect(storage.db_path("dota2"))
    print("links:", dict(dota_join.link_maps(conn)), flush=True)
    for label, where in (("all", ""), ("since 2025-10", "AND e.start_time >= '2025-10'"),
                         ("since 2026-06", "AND e.start_time >= '2026-06'")):
        n = conn.execute(f"""SELECT COUNT(*), SUM(l.od_match_id IS NOT NULL), SUM(l.cs_match_id IS NOT NULL)
                             FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
                             LEFT JOIN dota_map_links l ON l.pm_game_id = e.game_id AND l.map_number = m.map_number
                             WHERE m.market_type = 'child_moneyline' AND m.final_price_a IN (0, 1) {where}""").fetchone()
        print(f"Polymarket map markets ({label}): {n[0]}, with OpenDota game {n[1]}, with cybersport match {n[2]}")
    conn.close()


if __name__ == "__main__":
    main()
