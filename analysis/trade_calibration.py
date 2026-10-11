"""Calibration of real taker trades: does buying at price p win more or less often than p? (A-35)

Every taker trade is a bet: BUY of a token at p is a bet on it at p, SELL of a token at p is a bet on the other
side at 1 - p. Markets: resolved moneyline / child_moneyline with collected trades. Split pre-match / live by the
event start time. ROI is per trade (flat stake) after the taker fee; the error is a bootstrap over markets, since
trades of one market share one outcome.

If heavy favourites are underpriced everywhere, the R-8 / R-12 edges are just this bias, not the match state.

  python analysis/trade_calibration.py [--min-usd 0]
"""

import argparse
import random
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stage_calibration import tier_of  # noqa: E402

SEGMENTS = ("all", "tier1", "not tier1", "volume >= $200k", "volume < $200k")

BUCKETS = [(0.02, 0.10), (0.10, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 0.85), (0.85, 0.92), (0.92, 0.96),
           (0.96, 0.99)]
BOOT = 300


def bucket(p: float) -> int | None:
    for i, (lo, hi) in enumerate(BUCKETS):
        if lo <= p < hi:
            return i
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-usd", type=float, default=0.0, help="ignore trades smaller than this notional")
    args = ap.parse_args()
    conn = storage.connect()
    markets = conn.execute(
        """SELECT m.condition_id, m.token_a, m.token_b, m.final_price_a, e.start_time, m.volume, e.tournament
           FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
           WHERE m.trades_status = 'done' AND m.final_price_a IS NOT NULL
             AND (m.final_price_a >= 0.99 OR m.final_price_a <= 0.01)""").fetchall()
    print(f"resolved markets with trades: {len(markets)}")

    # (phase, segment, bucket) -> market -> [n, sum price, sum wins, sum taker return, sum maker return]
    agg: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0]))
    n_trades = 0
    for cond, tok_a, tok_b, final_a, start, volume, tournament in markets:
        try:
            start_ts = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError):
            continue
        a_won = final_a >= 0.99
        segments = ("all", "tier1" if tier_of(tournament or "") == "tier1" else "not tier1",
                    "volume >= $200k" if (volume or 0) >= 200_000 else "volume < $200k")
        for t, asset, side, price, size in conn.execute(
                "SELECT t, asset, side, price, size FROM pm_trades WHERE condition_id = ?", (cond,)):
            if price * size < args.min_usd:
                continue
            on_a = (asset == tok_a) == (side == "BUY")
            p = price if side == "BUY" else 1 - price
            b = bucket(p)
            if b is None:
                continue
            won = a_won == on_a
            ret = (1 if won else 0) / (p + 0.05 * p * (1 - p)) - 1
            maker = (0 if won else 1) / (1 - p) - 1  # resting order on the other side at 1 - p, no fee
            phase = "live" if t >= start_ts else "pre-match"
            for segment in segments:
                cell = agg[(phase, segment, b)][cond]
                cell[0] += 1
                cell[1] += p
                cell[2] += won
                cell[3] += ret
                cell[4] += maker
            n_trades += 1
    print(f"taker trades used: {n_trades}\n")

    rng = random.Random(1)
    for phase in ("pre-match", "live"):
        for segment in SEGMENTS:
            print(f"{phase} / {segment}")
            print(f"   {'taker p':11} {'trades':>8} {'mkts':>5} {'avg p':>6} {'win':>6} {'taker ROI':>15} "
                  f"{'maker ROI':>15}  (maker = other side at 1 - p)")
            for b, (lo, hi) in enumerate(BUCKETS):
                cells = list(agg.get((phase, segment, b), {}).values())
                n = sum(c[0] for c in cells)
                if n < 50:
                    continue
                boots = []
                for _ in range(BOOT):
                    pick = [cells[rng.randrange(len(cells))] for _ in cells]
                    k = sum(c[0] for c in pick)
                    boots.append((sum(c[3] for c in pick) / k, sum(c[4] for c in pick) / k))
                cols = []
                for j in (3, 4):
                    est = sum(c[j] for c in cells) / n
                    xs = [x[j - 3] for x in boots]
                    m = sum(xs) / BOOT
                    se = (sum((x - m) ** 2 for x in xs) / (BOOT - 1)) ** 0.5
                    cols.append(f"{est:+7.1%} ±{se:5.1%}")
                print(f"   {lo:.2f}-{hi:.2f}  {n:8d} {len(cells):5d} {sum(c[1] for c in cells) / n:6.3f} "
                      f"{sum(c[2] for c in cells) / n:6.1%} {cols[0]:>15} {cols[1]:>15}")
            print()


if __name__ == "__main__":
    main()
