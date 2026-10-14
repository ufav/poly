"""How often the underdog (price <= 0.50 at scheduled start, decimal odds >= 2.0) wins, by tier and odds bucket.
Match-winner and map-winner markets; ROI per $1 at that price after the taker fee 0.05*p*(1-p).

  python analysis/underdog_rate.py [--sport dota2] [--min-volume 1000]
"""

import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402
from pmbot.tiers import tier_of  # noqa: E402

BUCKETS = [(2.0, 2.5), (2.5, 3.33), (3.33, 5.0), (5.0, 100.0)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="dota2", choices=storage.SPORTS)
    ap.add_argument("--min-volume", type=float, default=1000)
    args = ap.parse_args()

    conn = storage.connect(storage.db_path(args.sport))
    rows = conn.execute(
        """SELECT m.market_type, m.token_a, m.final_price_a, e.start_time, e.tournament, e.best_of
           FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
           WHERE m.market_type IN ('moneyline', 'child_moneyline') AND m.history_status = 'done' AND m.closed = 1
             AND m.final_price_a IN (0, 1) AND m.volume >= ? AND e.start_time < ?""",
        (args.min_volume, dt.datetime.now(dt.timezone.utc).isoformat())).fetchall()

    agg = defaultdict(lambda: [0, 0, 0.0, 0.0])  # n, wins, sum price, sum return
    first, last = None, None
    for market_type, token, final_a, start_time, tournament, bo in rows:
        start = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
        pre = conn.execute("SELECT p FROM pm_prices WHERE token_id = ? AND t <= ? ORDER BY t DESC LIMIT 1",
                           (token, start)).fetchone()
        if not pre or not 0.01 < pre[0] < 0.99:
            continue
        p, won = (pre[0], final_a == 1) if pre[0] <= 0.5 else (1 - pre[0], final_a == 0)
        odds = 1 / p
        tier = tier_of(tournament, args.sport)
        kind = "match" if market_type == "moneyline" else "map"
        ret = (1 if won else 0) / (p + 0.05 * p * (1 - p)) - 1
        first = min(first or start_time, start_time)
        last = max(last or start_time, start_time)
        for lo, hi in BUCKETS:
            if lo <= odds < hi:
                for key in ((tier, kind, lo), (tier, kind, "all")):
                    a = agg[key]
                    a[0] += 1
                    a[1] += won
                    a[2] += p
                    a[3] += ret

    print(f"{args.sport}: underdog = side priced <= 0.50 at scheduled start (odds >= 2.0), "
          f"volume >= ${args.min_volume:,.0f} | {first[:10]} .. {last[:10]}\n")
    for tier in ("tier1", "tier2+"):
        for kind in ("match", "map"):
            print(f"{tier} {kind}")
            print(f"  {'odds':>11} {'n':>5} {'wins':>6} {'implied':>8} {'edge':>7} {'ROI':>7}")
            for lo, hi in BUCKETS + [("all", None)]:
                n, w, s, r = agg[(tier, kind, lo)]
                if not n:
                    continue
                label = "all >= 2.0" if lo == "all" else (f"{lo:.2f}-{hi:.2f}" if hi < 100 else f"{lo:.2f}+")
                print(f"  {label:>11} {n:5d} {w / n:6.1%} {s / n:8.1%} {w / n - s / n:+7.1%} {r / n:+7.1%}")
            print()


if __name__ == "__main__":
    main()
