"""Hypothesis A-2 (Polymarket prices only): buy a clear pre-match favorite's map when its in-play map price
first drops to a threshold. Compare realized win rate with the entry price, net of the taker fee.

Entry is simulated `--delay` minutes after the first touch (price must still be <= X + 0.05),
plus `--slippage` on top of the history price (history price is not the executable ask).

  python analysis/favorite_dip.py [--fav 0.70] [--min-volume 2000] [--delay 2] [--slippage 0.02]
"""

import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402

TAKER_FEE_RATE = 0.05
THRESHOLDS = (0.60, 0.50, 0.40, 0.30, 0.20, 0.10)


def fee(p: float) -> float:
    return TAKER_FEE_RATE * p * (1 - p)


def new_bucket() -> dict:
    return {"n": 0, "wins": 0, "cost": 0.0, "fees": 0.0}


def add(s: dict, entry: float, won: bool) -> None:
    s["n"] += 1
    s["wins"] += won
    s["cost"] += entry
    s["fees"] += fee(entry)


def print_table(title: str, stats: dict) -> None:
    print(title)
    print(f"{'X':>5} {'trades':>7} {'avg entry':>10} {'win rate':>9} {'edge':>7} {'edge-fee':>9} {'ROI':>7}")
    for x in THRESHOLDS:
        s = stats.get(x)
        if not s or not s["n"]:
            continue
        entry = s["cost"] / s["n"]
        win = s["wins"] / s["n"]
        net = win - entry - s["fees"] / s["n"]
        print(f"{x:5.2f} {s['n']:7d} {entry:10.3f} {win:9.1%} {win - entry:+7.3f} {net:+9.3f} {net / entry:+7.1%}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fav", type=float, default=0.70, help="min pre-match match-winner price of the favorite")
    ap.add_argument("--min-volume", type=float, default=2000, help="min map market volume, USD")
    ap.add_argument("--delay", type=int, default=2, help="minutes between first touch and entry")
    ap.add_argument("--slippage", type=float, default=0.02, help="added to history price at entry")
    args = ap.parse_args()

    conn = storage.connect()
    events = conn.execute(
        """SELECT e.id, e.start_time, m.token_a
           FROM pm_events e JOIN pm_markets m ON m.event_id = e.id AND m.market_type = 'moneyline'
           WHERE m.history_status = 'done' AND e.start_time < ?""",
        (dt.datetime.now(dt.timezone.utc).isoformat(),),
    ).fetchall()

    total = defaultdict(new_bucket)
    by_map = defaultdict(lambda: defaultdict(new_bucket))
    n_fav_maps = fav_map_wins = 0
    for event_id, start_time, ml_token in events:
        start = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
        pre = conn.execute("SELECT p FROM pm_prices WHERE token_id = ? AND t <= ? ORDER BY t DESC LIMIT 1",
                           (ml_token, start)).fetchone()
        if not pre:
            continue
        if pre[0] >= args.fav:
            fav_is_a = True
        elif 1 - pre[0] >= args.fav:
            fav_is_a = False
        else:
            continue

        maps = conn.execute(
            """SELECT token_a, final_price_a, map_number FROM pm_markets
               WHERE event_id = ? AND market_type = 'child_moneyline' AND history_status = 'done'
                 AND closed = 1 AND volume >= ? AND final_price_a IN (0, 1)""",
            (event_id, args.min_volume),
        ).fetchall()
        for map_token, final_a, map_no in maps:
            fav_won = (final_a == 1) == fav_is_a
            n_fav_maps += 1
            fav_map_wins += fav_won
            path = [(t, p if fav_is_a else 1 - p) for t, p in conn.execute(
                "SELECT t, p FROM pm_prices WHERE token_id = ? AND t >= ? ORDER BY t", (map_token, start))]
            done = set()
            for i, (t, fav_p) in enumerate(path):
                if fav_p <= 0.02:
                    break
                for x in THRESHOLDS:
                    if x in done or fav_p > x:
                        continue
                    done.add(x)
                    later = next((p for tt, p in path[i:] if tt >= t + args.delay * 60), None)
                    if later is None or later > x + 0.05 or later <= 0.02:
                        continue
                    entry = min(later + args.slippage, 0.99)
                    add(total[x], entry, fav_won)
                    add(by_map[map_no][x], entry, fav_won)

    print(f"favorite >= {args.fav:.2f} pre-match | map volume >= ${args.min_volume:,.0f} | "
          f"entry {args.delay} min after touch + {args.slippage:.2f} slippage")
    print(f"favorite maps: {n_fav_maps}, favorite won {fav_map_wins} ({fav_map_wins / max(n_fav_maps, 1):.1%})\n")
    print_table("ALL MAPS: buy favorite's map when its price first drops to <= X", total)
    for map_no in sorted(k for k in by_map if k):
        print_table(f"MAP {map_no}", by_map[map_no])


if __name__ == "__main__":
    main()
