"""Price-only in-play rule (no live score needed): buy a team on its map market when its price first
reaches X after the map has been running for >= M minutes; hold to settlement.

Entry = the next price point after the crossing (simulates ~1 min delay) + slippage + taker fee.

  python analysis/price_threshold.py [--slippage 0.01]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402
from halftime_price import collect  # noqa: E402

LEVELS = (0.70, 0.75, 0.80, 0.85, 0.90)
MIN_ELAPSED = (0, 10, 20, 30)


def simulate(windows: list[dict], level: float, min_elapsed: int, slippage: float, max_entry: float = 0.97):
    trades = []
    for w in windows:
        for side in ("ct", "t"):
            pts = [(t, p if side == "ct" else 1 - p) for t, p in w["path"]]
            won = w["ct_won"] if side == "ct" else not w["ct_won"]
            start = w["t0"] + min_elapsed * 60
            for i in range(1, len(pts) - 1):
                t, p = pts[i]
                if t >= start and p >= level > pts[i - 1][1]:
                    entry = pts[i + 1][1]
                    if entry < max_entry:
                        trades.append((entry, won, w["date"]))
                    break
    return trades


def summary(trades: list, slippage: float) -> str:
    if not trades:
        return "n=0"
    n = len(trades)
    cost = sum(p + slippage + 0.05 * p * (1 - p) for p, _, _ in trades)
    wins = sum(w for _, w, _ in trades)
    avg_p = sum(p for p, _, _ in trades) / n
    se2 = 2 * (wins / n * (1 - wins / n) / n) ** 0.5
    return (f"n={n:5d}  price {avg_p:.3f}  win {wins / n:6.1%} ±{se2:4.1%}  edge {wins / n - avg_p:+.3f}  "
            f"ROI {(wins - cost) / cost:+6.2%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slippage", type=float, default=0.01)
    args = ap.parse_args()
    conn = storage.connect()
    windows: list[dict] = []
    collect(conn, (0,), windows)
    print(f"timed maps: {len(windows)}\n")
    print("buy when price first crosses X after M minutes of the map (entry = next point, hold to the end)")
    for m in MIN_ELAPSED:
        for level in LEVELS:
            trades = simulate(windows, level, m, args.slippage)
            print(f"   M={m:2d} min  X={level:.2f}  {summary(trades, args.slippage)}")
        print()

    print("robustness for M=20, X=0.80: by period")
    trades = simulate(windows, 0.80, 20, args.slippage)
    for label, cond in (("Sep 2025 - Mar 2026", lambda d: d < "2026-04-01"), ("Apr 2026 - Oct 2026", lambda d: d >= "2026-04-01")):
        print(f"   {label:22} {summary([x for x in trades if cond(x[2])], args.slippage)}")


if __name__ == "__main__":
    main()
