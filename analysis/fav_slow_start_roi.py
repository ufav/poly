"""CS2: ROI of buying the clear favourite (match prematch >= 0.60) on the map market when he is down early
(0-3 ... 0-6, 1-4 ... 1-6), and of buying the leading underdog instead. Timing as R-8: true map begin/end from
PandaScore, rounds spread evenly; entry = estimated end of the round + delay, price = Polymarket history (not ask),
net of taker fee 0.05*p*(1-p) and slippage.

  python analysis/fav_slow_start_roi.py [--entry-delay 60] [--slippage 0.01]
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmbot import storage  # noqa: E402
from pmbot.maptiming import price_at, price_series  # noqa: E402
from halftime_price import collect  # noqa: E402
from fav_slow_start import FAV_BANDS, STATES, band_of  # noqa: E402


def roi(items: list[tuple[float, bool]], slippage: float) -> tuple[int, float, float, float]:
    """(n, mean price, win rate, ROI) of buying at the given prices."""
    n = len(items)
    cost = sum(p + slippage + 0.05 * p * (1 - p) for p, _ in items)
    wins = sum(w for _, w in items)
    return n, sum(p for p, _ in items) / n, wins / n, (wins - cost) / cost


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry-delay", type=int, default=60, help="seconds after the estimated end of the round")
    ap.add_argument("--slippage", type=float, default=0.01)
    args = ap.parse_args()
    conn = storage.connect()
    rows, _ = collect(conn, (0,))
    series_cache: dict[str, list] = {}
    dates = sorted(r["date"] for r in rows)
    split_date = dates[len(dates) // 2]

    # (scope, band, state) -> [(fav price, fav won)]; scope 'all' / 'tier1', band None = any favourite
    data = defaultdict(list)
    n_maps = 0
    for r in rows:
        band = band_of(r["pre"])
        if not band:
            continue
        n_maps += 1
        seq = r["seq_all"]
        n_rounds = len(seq)
        token = r["token"]
        if token not in series_cache:
            series_cache[token] = price_series(conn, token)
        series = series_cache[token]
        score = [0, 0]
        reached = {}
        for k, fav_won in enumerate(seq, 1):
            score[0 if fav_won else 1] += 1
            reached.setdefault(tuple(score), k)
        first = True
        for st in sorted(STATES, key=lambda s: reached.get(s, 99)):
            k = reached.get(st)
            if not k:
                continue
            p = price_at(series, int(r["t0"] + (r["t1"] - r["t0"]) * k / n_rounds + args.entry_delay))
            if p is None or not 0.01 < p < 0.99:
                continue
            for scope in ("all", "tier1") if r["tier"] == "tier1" else ("all",):
                for b in (band, None):
                    data[(scope, b, st)].append((p, r["won"]))
                    if first:  # one entry per map: the first state it reaches
                        data[(scope, b, "first")].append((p, r["won"]))
                        data[(scope, b, "first", r["date"] < split_date)].append((p, r["won"]))
            first = False

    print(f"favourite maps (prematch >= 0.60) with PandaScore timing: {n_maps}; entry +{args.entry_delay}s, "
          f"slippage {args.slippage}")
    for scope in ("all", "tier1"):
        for b in (None, *FAV_BANDS):
            name = "all fav >= 0.60" if b is None else f"fav {b[0]:.2f}-{b[1]:.2f}"
            print(f"\n{scope}, {name}")
            print(f"  {'fav-dog':>7} {'n':>5} {'price':>6} {'win':>6} {'ROI fav':>8} {'ROI dog':>8}")
            for st in STATES:
                items = data[(scope, b, st)]
                if len(items) < 10:
                    continue
                n, price, win, r_fav = roi(items, args.slippage)
                _, _, _, r_dog = roi([(1 - p, not w) for p, w in items], args.slippage)
                print(f"  {st[0]}-{st[1]:<5} {n:5d} {price:6.3f} {win:6.1%} {r_fav:+8.1%} {r_dog:+8.1%}")
            for key, label in (("first",), "1 entry/map"), (("first", True), f"  < {split_date}"), \
                              (("first", False), f"  >= {split_date}"):
                items = data[(scope, b, *key)]
                if len(items) < 10:
                    continue
                n, price, win, r_fav = roi(items, args.slippage)
                _, _, _, r_dog = roi([(1 - p, not w) for p, w in items], args.slippage)
                se = 2 * (win * (1 - win) / n) ** 0.5
                print(f"  {label:<13} n={n:4d} price {price:5.3f} win {win:6.1%} ±{se:5.1%} "
                      f"ROI fav {r_fav:+6.1%}  dog {r_dog:+6.1%}")


if __name__ == "__main__":
    main()
