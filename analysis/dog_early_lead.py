"""CS2: buy the match underdog on the map market as soon as he leads early (first time at 2-0 / 3-0 / 3-1) and sell
when the stake has grown x1.5 / x2 / x3 at any point of the map, else hold to settlement. Timing as R-8 (PandaScore
map window, rounds spread evenly), entry at the estimated end of the round + delay; Polymarket minute history (not
ask), fee 0.05*p*(1-p) and slippage on both sides. Example: Nemiga - Vitality 30.10.2026 map 2 (bought at 2-0,
sold at x3).

  python analysis/dog_early_lead.py [--entry-delay 60] [--slippage 0.01] [--target 2]
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

STATES = ((2, 0), (3, 0), (3, 1))   # dog-fav round score
DOG_MAX_PRE = 0.45                   # match prematch of the underdog (odds >= 2.22)
ENTRY_ODDS = ((1.0, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 99.0))  # map odds at entry
MULTS = (1.5, 2.0, 3.0)


def fee(p: float) -> float:
    return 0.05 * p * (1 - p)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry-delay", type=int, default=60)
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--target", type=float, default=3.0, choices=MULTS, help="exit shown by halves / hit rate")
    args = ap.parse_args()
    slip = args.slippage

    def sell(p: float) -> float:
        p = max(p - slip, 0.0)
        return p - fee(p)

    conn = storage.connect()
    rows, _ = collect(conn, (0,))
    dates = sorted(r["date"] for r in rows)
    split = dates[len(dates) // 2]
    series_cache: dict[str, list] = {}
    data = defaultdict(list)  # (scope, state, odds band or None, period or None) -> [(cost, {exit: proceeds})]
    for r in rows:
        if r["pre"] > DOG_MAX_PRE:
            continue
        seq, n = r["seq_all"], len(r["seq_all"])
        score, reached = [0, 0], {}
        for k, won_round in enumerate(seq, 1):
            score[0 if won_round else 1] += 1
            reached.setdefault(tuple(score), k)
        if r["token"] not in series_cache:
            series_cache[r["token"]] = price_series(conn, r["token"])
        series = series_cache[r["token"]]
        for st in STATES:
            k = reached.get(st)
            if not k:
                continue
            t_in = int(r["t0"] + (r["t1"] - r["t0"]) * k / n + args.entry_delay)
            p_in = price_at(series, t_in)
            if p_in is None or not 0.01 < p_in < 0.99:
                continue
            cost = p_in + slip + fee(p_in + slip)
            path = [p for t, p in series if t_in < t <= r["t1"]]
            settle = 1.0 if r["won"] else 0.0
            res = {"hold": settle}
            for m in MULTS:
                hit = next((p for p in path if sell(p) >= m * cost), None)
                res[f"x{m:g}"] = sell(hit) if hit is not None else settle
            band = next((b for b in ENTRY_ODDS if b[0] <= 1 / p_in < b[1]), None)
            for sc in ("all", "tier1") if r["tier"] == "tier1" else ("all",):
                for b in (band, None):
                    for period in (None, r["date"] < split):
                        data[(sc, st, b, period)].append((cost, res))

    exits = ["hold", *(f"x{m:g}" for m in MULTS)]
    print(f"underdog = match prematch odds >= {1 / DOG_MAX_PRE:.2f}; entry +{args.entry_delay}s; "
          f"slippage {slip}; halves split at {split}")
    for sc in ("all", "tier1"):
        print(f"\n{sc}")
        print(f"  {'score':<6} {'map odds':<9} {'n':>5} {'odds':>5} {'won':>6} "
              + " ".join(f"{e:>7}" for e in exits) + f" {'x' + format(args.target, 'g') + ' before/after':>17} {'hit':>7}")
        for st in STATES:
            for b in (None, *ENTRY_ODDS):
                items = data[(sc, st, b, None)]
                if len(items) < 10:
                    continue
                cost = sum(c for c, _ in items)
                odds = sum(1 / (c - slip - fee(c - slip)) for c, _ in items) / len(items)
                won = sum(1 for _, x in items if x["hold"] == 1.0) / len(items)
                rois = " ".join(f"{sum(x[e] for _, x in items) / cost - 1:+7.1%}" for e in exits)

                tgt = f"x{args.target:g}"

                def roi_t(xs):
                    return f"{sum(x[tgt] for _, x in xs) / sum(c for c, _ in xs) - 1:+.0%}" if xs else "—"
                halves = f"{roi_t(data[(sc, st, b, True)])} / {roi_t(data[(sc, st, b, False)])}"
                hit3 = sum(1 for _, x in items if 0 < x[tgt] != 1.0) / len(items)
                label = "any" if b is None else (f"{b[0]:g}-{b[1]:g}" if b[1] < 99 else f"{b[0]:g}+")
                print(f"  {st[0]}-{st[1]:<4} {label:<9} {len(items):5d} {odds:5.2f} {won:6.1%} {rois} "
                      f"{halves:>17} {hit3:7.0%}")


if __name__ == "__main__":
    main()
