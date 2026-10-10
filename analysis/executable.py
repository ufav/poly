"""Re-check R-8 / R-12 signals on real taker trades instead of the price-history midpoint.

For each signal (entry time T = estimated state time + 60 s, PandaScore timing) look at taker trades of the
market in [T, T + --window]: a buy of the team token at p, or a sale of the other token at q (= buying the team
at 1 - q), is an executable buy price. Reported: share of signals with any such trade, with >= $100 of it,
history price vs first trade price, ROI on the history price (+1 c) vs on the first trade price (fee only).
Only markets whose trades were collected (scripts/collect_trades.py) are used.

  python analysis/executable.py [--window 180]
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmbot import storage  # noqa: E402
from halftime_price import collect  # noqa: E402
from near_certain import entry  # noqa: E402

STAKE = 100.0
OFFSET = 60


def buys(conn, row: dict, t: float, window: int) -> list[tuple[int, float, float]]:
    """(time, price for the team, notional $) of buy-equivalent taker trades, chronological."""
    out = []
    for ts, asset, side, price, size in conn.execute(
            "SELECT t, asset, side, price, size FROM pm_trades WHERE condition_id = ? AND t BETWEEN ? AND ? ORDER BY t",
            (row["cond"], int(t), int(t + window))):
        if asset == row["token"] and side == "BUY":
            out.append((ts, price, price * size))
        elif asset == row["token_other"] and side == "SELL":
            out.append((ts, 1 - price, (1 - price) * size))
    return out


def roi(items: list[tuple[float, bool]], slippage: float) -> float:
    cost = sum(p + slippage + 0.05 * p * (1 - p) for p, _ in items)
    return (sum(w for _, w in items) - cost) / cost


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=180)
    args = ap.parse_args()
    conn = storage.connect()
    collected = {c for (c,) in conn.execute("SELECT condition_id FROM pm_markets WHERE trades_status IS NOT NULL")}
    rows, _ = collect(conn, (OFFSET,))
    rows = [r for r in rows if r["cond"] in collected]
    print(f"team-map rows with collected trades: {len(rows)}\n")

    signals = defaultdict(list)  # rule -> [(row, T, history price)]
    for r in rows:
        if r["lead"] >= 6 or (r["lead"] == 4 and r["first_side"] == "T"):
            signals["R-8 leader at half"].append((r, r["t_half"] + OFFSET, r["price"][OFFSET]))
        e = entry(r, 5, 11, OFFSET)
        if e:
            n = len(r["seq_all"])
            signals["R-12 lead>=5, rounds>=11"].append((r, r["t0"] + (r["t1"] - r["t0"]) * e[1] / n + OFFSET, e[0]))

    for rule, items in signals.items():
        print(f"{rule}  (window {args.window}s)")
        for tier in ("all", "tier1", "not tier1"):
            sel = [x for x in items if tier == "all" or (x[0]["tier"] == "tier1") == (tier == "tier1")]
            if len(sel) < 20:
                continue
            traded, full, hist_pts, exec_pts, gaps = 0, 0, [], [], []
            for r, t, p_hist in sel:
                b = buys(conn, r, t, args.window)
                if not b:
                    continue
                traded += 1
                full += sum(x[2] for x in b) >= STAKE
                hist_pts.append((p_hist, r["won"]))
                exec_pts.append((b[0][1], r["won"]))
                gaps.append(b[0][1] - p_hist)
            n = len(sel)
            line = f"   {tier:9} signals {n:4d}  with a buy trade {traded / n:5.1%}  with >= ${STAKE:.0f} {full / n:5.1%}"
            if exec_pts:
                k = len(exec_pts)
                line += (f" | traded subset n={k}: hist {sum(p for p, _ in hist_pts) / k:.3f} -> trade "
                         f"{sum(p for p, _ in exec_pts) / k:.3f} (gap {sum(gaps) / k:+.3f}), win "
                         f"{sum(w for _, w in exec_pts) / k:.1%}, ROI hist {roi(hist_pts, 0.01):+.1%} "
                         f"-> on trades {roi(exec_pts, 0.0):+.1%}")
            print(line)
        print()


if __name__ == "__main__":
    main()
