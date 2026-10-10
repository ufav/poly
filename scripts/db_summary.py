"""Print database stats and the price path of one map market (sanity check)."""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402

conn = storage.connect()
q = lambda sql, *a: conn.execute(sql, a).fetchall()

print("events:", q("SELECT COUNT(*), MIN(start_time), MAX(start_time) FROM pm_events")[0])
print("history status:", q("SELECT history_status, COUNT(*) FROM pm_markets GROUP BY 1"))
print("market types:", q("SELECT market_type, COUNT(*) FROM pm_markets GROUP BY 1 ORDER BY 2 DESC"))
print("price points:", q("SELECT COUNT(*) FROM pm_prices")[0][0])

row = q("""SELECT m.token_a, m.outcome_a, m.outcome_b, m.final_price_a, e.title, e.start_time, m.group_title
           FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
           WHERE m.history_status = 'done' AND m.market_type = 'child_moneyline'
           ORDER BY e.volume DESC LIMIT 1""")
if row:
    token, a, b, final_a, title, start, group = row[0]
    print(f"\n{title[:90]} | {group} | {a} vs {b} | final {a} = {final_a}")
    start_ts = dt.datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
    pts = q("SELECT t, p FROM pm_prices WHERE token_id = ? ORDER BY t", token)
    for t, p in pts[:: max(1, len(pts) // 30)]:
        print(f"  {(t - start_ts) / 60:+7.0f} min  {p:.3f}")
