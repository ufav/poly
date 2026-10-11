"""Market types in a game's database: count, with map number, volume, trades status.

  python scripts/research/market_types.py [dota2]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot import storage  # noqa: E402

conn = storage.connect(storage.db_path(sys.argv[1] if len(sys.argv) > 1 else "cs2"))
for row in conn.execute(
        """SELECT market_type, COUNT(*), SUM(map_number IS NOT NULL), ROUND(SUM(volume) / 1e6, 1),
                  SUM(trades_status IS NOT NULL)
           FROM pm_markets WHERE market_type IN ('moneyline', 'child_moneyline') GROUP BY 1"""):
    print("type {}: markets {}, with map number {}, volume {}M, trades collected {}".format(*row))
