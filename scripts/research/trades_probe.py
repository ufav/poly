"""Data API /trades for one CS2 map market: fields, paging, how many trades.

  python scripts/research/trades_probe.py [condition_id]
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot import storage  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402

API = "https://data-api.polymarket.com/trades"


def main() -> None:
    conn = storage.connect()
    cond = sys.argv[1] if len(sys.argv) > 1 else conn.execute(
        """SELECT m.condition_id FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
           WHERE m.market_type = 'child_moneyline' AND e.title LIKE '%Vitality%' AND m.closed = 1
           ORDER BY e.start_time DESC LIMIT 1""").fetchone()[0]
    print("condition:", cond)
    http = HttpClient()
    total, offset = 0, 0
    while True:
        r = http.get(API, {"market": cond, "limit": 500, "offset": offset, "takerOnly": "true"})
        print("status", r.status_code)
        if r.status_code != 200:
            print(r.text[:300])
            break
        batch = r.json()
        if offset == 0 and batch:
            print(json.dumps(batch[0], indent=1)[:1500])
        total += len(batch)
        if len(batch) < 500:
            break
        offset += 500
    print("trades:", total)


if __name__ == "__main__":
    main()
