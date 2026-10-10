"""BO3 map markets coverage: child_moneyline per map number, settled or not; pm_prices index.

  python scripts/research/bo3_maps_check.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot import storage  # noqa: E402


def main() -> None:
    conn = storage.connect()
    print(conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name = 'pm_prices'").fetchall())
    for row in conn.execute(
            """SELECT map_number, COUNT(*), SUM(history_status = 'done'), SUM(final_price_a IS NOT NULL),
                      SUM(final_price_a > 0.05 AND final_price_a < 0.95)
               FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
               WHERE e.best_of = 3 AND market_type = 'child_moneyline' GROUP BY map_number"""):
        print("map", row[0], "markets", row[1], "history done", row[2], "final set", row[3], "unsettled", row[4])
    print("ps_games positions:", conn.execute(
        "SELECT position, COUNT(*), SUM(begin_at IS NOT NULL) FROM ps_games GROUP BY position").fetchall())


if __name__ == "__main__":
    main()
