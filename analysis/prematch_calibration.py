"""Calibration of prices at scheduled match start: price bucket vs realized win rate,
for the match-winner market and for map-winner markets, from the favorite's side.

  python analysis/prematch_calibration.py [--min-volume 2000]
"""

import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402

BUCKETS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.98)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-volume", type=float, default=2000)
    args = ap.parse_args()

    conn = storage.connect()
    rows = conn.execute(
        """SELECT m.market_type, m.token_a, m.final_price_a, e.start_time
           FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
           WHERE m.market_type IN ('moneyline', 'child_moneyline') AND m.history_status = 'done'
             AND m.closed = 1 AND m.final_price_a IN (0, 1) AND m.volume >= ? AND e.start_time < ?""",
        (args.min_volume, dt.datetime.now(dt.timezone.utc).isoformat()),
    ).fetchall()

    agg = defaultdict(lambda: [0, 0, 0.0])
    for market_type, token, final_a, start_time in rows:
        start = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
        pre = conn.execute("SELECT p FROM pm_prices WHERE token_id = ? AND t <= ? ORDER BY t DESC LIMIT 1",
                           (token, start)).fetchone()
        if not pre:
            continue
        p, won = (pre[0], final_a == 1) if pre[0] >= 0.5 else (1 - pre[0], final_a == 0)
        for lo, hi in BUCKETS:
            if lo <= p < hi:
                a = agg[(market_type, lo)]
                a[0] += 1
                a[1] += won
                a[2] += p

    print(f"price of the favorite at scheduled start | market volume >= ${args.min_volume:,.0f}\n")
    for market_type in ("moneyline", "child_moneyline"):
        print("MATCH WINNER" if market_type == "moneyline" else "MAP WINNER (all maps)")
        print(f"{'bucket':>11} {'n':>6} {'avg price':>10} {'win rate':>9} {'edge':>7}")
        for lo, hi in BUCKETS:
            n, w, s = agg[(market_type, lo)]
            if n:
                print(f"{lo:.2f}-{hi:.2f} {n:6d} {s / n:10.3f} {w / n:9.1%} {w / n - s / n:+7.3f}")
        print()


if __name__ == "__main__":
    main()
