"""Trades per collected market (cap check) and a deep-offset probe of the Data API.

  python scripts/research/trades_check.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot import polymarket, storage  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402


def main() -> None:
    conn = storage.connect()
    rows = conn.execute(
        """SELECT m.market_type, m.map_number, e.title, COUNT(t.t), MIN(t.t), MAX(t.t), m.condition_id
           FROM pm_markets m JOIN pm_events e ON e.id = m.event_id LEFT JOIN pm_trades t ON t.condition_id = m.condition_id
           WHERE m.trades_status IS NOT NULL GROUP BY m.id ORDER BY COUNT(t.t) DESC""").fetchall()
    for r in rows[:12]:
        print(f"{r[3]:6d}  {r[0]:15} {str(r[1]):4} {r[2][:60]}")
    top = rows[0][6]
    http = HttpClient()
    for offset in (9500, 10000, 10500, 20000):
        resp = http.get(f"{polymarket.DATA}/trades", {"market": top, "limit": 500, "offset": offset, "takerOnly": "true"})
        print("offset", offset, resp.status_code, len(resp.json()) if resp.status_code == 200 else resp.text[:120])


if __name__ == "__main__":
    main()
