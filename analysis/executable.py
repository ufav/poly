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


def vwap(b: list[tuple[int, float, float]], stake: float) -> float | None:
    """Average price of the first `stake` dollars of the tape; None if the window traded less."""
    spent, shares = 0.0, 0.0
    for _, p, usd in b:
        take = min(usd, stake - spent)
        spent += take
        shares += take / p
        if spent >= stake - 1e-9:
            return spent / shares
    return None


def roi(items: list[tuple[float, bool]], slippage: float) -> tuple[float, float]:
    """Flat-stake ROI and its standard error."""
    rets = []
    for p, w in items:
        cost = p + slippage + 0.05 * p * (1 - p)
        rets.append((1 if w else 0) / cost - 1)
    k = len(rets)
    mean = sum(rets) / k
    var = sum((x - mean) ** 2 for x in rets) / max(k - 1, 1)
    return mean, (var / k) ** 0.5


def fmt(r: tuple[float, float]) -> str:
    return f"{r[0]:+.1%}±{r[1]:.1%}"


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
        p_half = r["price"].get(OFFSET)
        if p_half is not None and 0.70 <= p_half < 0.92:
            signals["control: any team priced 0.70-0.92 at half, score ignored"].append((r, r["t_half"] + OFFSET, p_half))
        e = entry(r, 5, 11, OFFSET)
        if e:
            n = len(r["seq_all"])
            signals["R-12 lead>=5, rounds>=11"].append((r, r["t0"] + (r["t1"] - r["t0"]) * e[1] / n + OFFSET, e[0]))

    all_t = sorted(t for items in signals.values() for _, t, _ in items)
    split_t = all_t[len(all_t) // 2] if all_t else 0
    groups = {
        "all": lambda r, t: True,
        "tier1": lambda r, t: r["tier"] == "tier1",
        "not tier1": lambda r, t: r["tier"] != "tier1",
        "1st half": lambda r, t: t < split_t,
        "2nd half": lambda r, t: t >= split_t,
    }
    for rule, items in signals.items():
        print(f"{rule}  (window {args.window}s; ROI ± standard error; '$100' = VWAP of the first ${STAKE:.0f} of tape)")
        tape = [(r, t, p_hist, buys(conn, r, t, args.window)) for r, t, p_hist in items]
        for name, keep in groups.items():
            sel = [x for x in tape if keep(x[0], x[1])]
            if len(sel) < 20:
                continue
            hist_pts, first_pts, full_pts, gaps = [], [], [], []
            for r, t, p_hist, b in sel:
                if not b:
                    continue
                hist_pts.append((p_hist, r["won"]))
                first_pts.append((b[0][1], r["won"]))
                gaps.append(b[0][1] - p_hist)
                v = vwap(b, STAKE)
                if v is not None:
                    full_pts.append((v, r["won"]))
            n, k = len(sel), len(first_pts)
            line = f"   {name:9} signals {n:4d}  traded {k / n:5.1%}  $100 filled {len(full_pts) / n:5.1%}"
            if k >= 20:
                line += (f" | gap {sum(gaps) / k:+.3f}  win {sum(w for _, w in first_pts) / k:.1%}  "
                         f"ROI hist {fmt(roi(hist_pts, 0.01))}  first trade {fmt(roi(first_pts, 0.0))}")
            if len(full_pts) >= 20:
                line += f"  $100 {fmt(roi(full_pts, 0.0))}"
            print(line)
        print()


if __name__ == "__main__":
    main()
