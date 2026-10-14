"""Back the underdog with odds in [--lo, --hi) at scheduled start (--tier tier1 / tier2+ / all), in start-time order;
flat % of bank vs x2 after a loss vs full chase (staking from top10_martingale.simulate). Ruin = share of
--shuffles random orders ending below 10% of the bank.

  python analysis/underdog_martingale.py [--sport dota2] [--lo 2.0] [--hi 2.5] [--kind match|map] [--bank 1000]
"""

import argparse
import datetime as dt
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmbot import storage  # noqa: E402
from pmbot.tiers import tier_of  # noqa: E402
from top10_martingale import simulate, streaks  # noqa: E402


def bets(conn, sport: str, kind: str, lo: float, hi: float, min_volume: float, tier: str = "tier1") -> list[dict]:
    market_type = "moneyline" if kind == "match" else "child_moneyline"
    out = []
    for token, final_a, start_time, tournament in conn.execute(
            """SELECT m.token_a, m.final_price_a, e.start_time, e.tournament
               FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
               WHERE m.market_type = ? AND m.history_status = 'done' AND m.closed = 1
                 AND m.final_price_a IN (0, 1) AND m.volume >= ?""", (market_type, min_volume)):
        t = tier_of(tournament, sport)
        if tier == "tier1" and t != "tier1" or tier == "tier2+" and t == "tier1":
            continue
        start = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
        pre = conn.execute("SELECT p FROM pm_prices WHERE token_id = ? AND t <= ? ORDER BY t DESC LIMIT 1",
                           (token, start)).fetchone()
        if not pre or not 0.01 < pre[0] < 0.99:
            continue
        p, won = (pre[0], final_a == 1) if pre[0] <= 0.5 else (1 - pre[0], final_a == 0)
        if lo <= 1 / p < hi:
            out.append({"t": start, "p": p, "won": won, "cost": p + 0.05 * p * (1 - p), "tournament": tournament})
    out.sort(key=lambda x: x["t"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="dota2", choices=storage.SPORTS)
    ap.add_argument("--kind", default="match", choices=("match", "map"))
    ap.add_argument("--lo", type=float, default=2.0)
    ap.add_argument("--hi", type=float, default=2.5)
    ap.add_argument("--min-volume", type=float, default=1000)
    ap.add_argument("--tier", default="tier1", choices=("tier1", "tier2+", "all"))
    ap.add_argument("--bank", type=float, default=1000)
    ap.add_argument("--shuffles", type=int, default=2000)
    args = ap.parse_args()

    items = bets(storage.connect(storage.db_path(args.sport)), args.sport, args.kind, args.lo, args.hi,
                 args.min_volume, args.tier)
    n, w = len(items), sum(x["won"] for x in items)
    day = lambda x: dt.datetime.fromtimestamp(x["t"], dt.timezone.utc).date()  # noqa: E731
    print(f"{args.sport} {args.tier} {args.kind}, underdog odds {args.lo}-{args.hi} at start, taker fee | "
          f"{day(items[0])} .. {day(items[-1])}")
    rets = [(1 / x["cost"] if x["won"] else 0) - 1 for x in items]
    mean = sum(rets) / n
    se = (sum((r - mean) ** 2 for r in rets) / (n - 1) / n) ** 0.5
    print(f"bets {n}  wins {w / n:.1%}  avg odds {sum(1 / x['p'] for x in items) / n:.2f}  flat ROI {mean:+.1%}±{se:.1%}")
    half = n // 2
    for name, sel in (("1st half", items[:half]), ("2nd half", items[half:])):
        r = [(1 / x["cost"] if x["won"] else 0) - 1 for x in sel]
        print(f"  {name}: {day(sel[0])} .. {day(sel[-1])}  bets {len(sel)}  wins "
              f"{sum(x['won'] for x in sel) / len(sel):.1%}  ROI {sum(r) / len(r):+.1%}")
    s = streaks(items)
    print("losing streaks (length: count):", ", ".join(f"{k}: {v}" for k, v in sorted(s.items())))

    print(f"\nbank ${args.bank:,.0f}; x2 = stake doubles after each loss, back to base after a win")
    for stake in (0.01, 0.02, 0.03, 0.05):
        for mode in ("flat", "x2"):
            r = simulate(items, args.bank, stake, mode)
            rng = random.Random(1)
            finals = []
            for _ in range(args.shuffles):
                order = items[:]
                rng.shuffle(order)
                finals.append(simulate(order, args.bank, stake, mode)["bank"])
            finals.sort()
            ruin = sum(f < 0.1 * args.bank for f in finals) / len(finals)
            bust = f"  BUST {dt.datetime.fromtimestamp(r['bust'], dt.timezone.utc).date()}" if r["bust"] else ""
            print(f"  base {stake:.0%} {mode:4}  final ${r['bank']:>9,.0f}  max drawdown {r['dd']:5.1%}  "
                  f"max bet {r['share']:5.1%} of bank{bust} | shuffled: median ${finals[len(finals) // 2]:,.0f}  "
                  f"p10 ${finals[len(finals) // 10]:,.0f}  ruin {ruin:.1%}")


if __name__ == "__main__":
    main()
