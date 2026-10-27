"""CS2: buy the clear favourite (match prematch >= 0.60) on the map market after a bad start (first of 0-3 ... 0-6,
1-4 ... 1-6 the map reaches), then sell into the comeback: take-profit / stop-loss on the minute price path, sell at
the half, or hold to settlement. Timing and costs as R-8 / R-29 (PandaScore map window, rounds spread evenly,
history price, taker fee 0.05*p*(1-p) on entry and sale, slippage on both sides).

  python analysis/fav_comeback_exit.py [--entry-delay 60] [--slippage 0.01]
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
from pmbot.join import norm_team  # noqa: E402

TP = (0.05, 0.10, 0.15, 0.20)
TP_SL = ((0.10, 0.10), (0.15, 0.10), (0.10, 0.15), (0.20, 0.15))


def fee(p: float) -> float:
    return 0.05 * p * (1 - p)


def exits(path: list[tuple[int, float]], p_in: float, won: bool, t_half: float, slippage: float) -> dict[str, float]:
    """Proceeds per share of every exit rule; path = (t, fav price) after entry up to the map end."""
    def sell(p: float) -> float:
        p = max(p - slippage, 0.0)
        return p - fee(p)

    settle = 1.0 if won else 0.0
    out = {"hold": settle}
    for tp in TP:
        hit = next((p for _, p in path if p >= p_in + tp), None)
        out[f"TP +{tp:.2f}"] = sell(hit) if hit is not None else settle
    for tp, sl in TP_SL:
        hit = next((p for _, p in path if p >= p_in + tp or p <= p_in - sl), None)
        out[f"TP +{tp:.2f} / SL -{sl:.2f}"] = sell(hit) if hit is not None else settle
    half = next((p for t, p in path if t >= t_half), None)
    out["sell at half"] = sell(half) if half is not None else settle
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry-delay", type=int, default=60, help="seconds after the estimated end of the round")
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--teams", help="comma list: favourite must be one of them and the opponent none of them")
    ap.add_argument("--list", action="store_true", help="print every entry")
    args = ap.parse_args()
    teams = {norm_team(t) for t in args.teams.split(",")} if args.teams else None
    conn = storage.connect()
    rows, _ = collect(conn, (0,))
    series_cache: dict[str, list] = {}
    dates = sorted(r["date"] for r in rows)
    split_date = dates[len(dates) // 2]
    map_teams = defaultdict(set)  # map -> both teams (collect yields a row per team)
    for r in rows:
        map_teams[(r["t0"], r["cond"])].add(norm_team(r["team"]))

    # (scope, band, period) -> list of (cost, {rule: proceeds}); band None = any favourite, period None = all
    data = defaultdict(list)
    entries = []
    for r in rows:
        band = band_of(r["pre"])
        if not band:
            continue
        if teams is not None:
            fav = norm_team(r["team"])
            opp = map_teams[(r["t0"], r["cond"])] - {fav}
            if fav not in teams or not opp or opp & teams:
                continue
        seq, n_rounds = r["seq_all"], len(r["seq_all"])
        score, reached = [0, 0], {}
        for k, fav_won in enumerate(seq, 1):
            score[0 if fav_won else 1] += 1
            reached.setdefault(tuple(score), k)
        ks = [reached[s] for s in STATES if s in reached]
        if not ks:
            continue
        k = min(ks)
        if k >= 12:  # the bad start must be in the first half
            continue
        if r["token"] not in series_cache:
            series_cache[r["token"]] = price_series(conn, r["token"])
        series = series_cache[r["token"]]
        t_in = int(r["t0"] + (r["t1"] - r["t0"]) * k / n_rounds + args.entry_delay)
        p_in = price_at(series, t_in)
        if p_in is None or not 0.01 < p_in < 0.99:
            continue
        cost = p_in + args.slippage + fee(p_in + args.slippage)
        path = [(t, p) for t, p in series if t_in < t <= r["t1"]]
        res = exits(path, p_in, r["won"], r["t_half"], args.slippage)
        if r["tier"] == "tier1":
            state = next(s for s in STATES if reached.get(s) == k)
            best = max((p for _, p in path), default=p_in)
            entries.append((r["date"], r["team"], "/".join(sorted(map_teams[(r["t0"], r["cond"])]
                                                               - {norm_team(r["team"])})),
                            f"{state[0]}-{state[1]}", 1 / r["pre"], 1 / p_in, 1 / best, r["won"]))
        for scope in ("all", "tier1") if r["tier"] == "tier1" else ("all",):
            for b in (band, None):
                for period in (None, r["date"] < split_date):
                    data[(scope, b, period)].append((cost, res))

    if args.list:
        print("tier1 entries: date, favourite vs, state, odds prematch / entry / best later, map won")
        for e in sorted(entries):
            print(f"  {e[0]} {e[1]:<16} vs {e[2]:<16} {e[3]:<4} {e[4]:5.2f} {e[5]:5.2f} {e[6]:5.2f} "
                  f"{'won' if e[7] else 'lost'}")
        if entries:
            print(f"  won {sum(e[7] for e in entries)}/{len(entries)}, mean entry odds "
                  f"{sum(e[5] for e in entries) / len(entries):.2f}")
    rules = list(next(iter(data.values()))[0][1])
    print(f"entry +{args.entry_delay}s, slippage {args.slippage}; halves split at {split_date}")
    for scope in ("all", "tier1"):
        for b in (None, *FAV_BANDS):
            items = data[(scope, b, None)]
            if len(items) < 15:
                continue
            name = "fav >= 0.60" if b is None else f"fav {b[0]:.2f}-{b[1]:.2f}"
            print(f"\n{scope}, {name}: n={len(items)} (before / after: "
                  f"{len(data[(scope, b, True)])} / {len(data[(scope, b, False)])})")
            print(f"  {'exit':<22} {'ROI':>7} {'before':>7} {'after':>7} {'sold':>7}")
            for rule in rules:
                def roi(xs):
                    c = sum(x[0] for x in xs)
                    return sum(x[1][rule] for x in xs) / c - 1 if xs else float("nan")
                hit = ""
                if rule.startswith("TP"):
                    hit = f"{sum(1 for c, x in items if x[rule] not in (0.0, 1.0)) / len(items):6.0%}"
                print(f"  {rule:<22} {roi(items):+7.1%} {roi(data[(scope, b, True)]):+7.1%} "
                      f"{roi(data[(scope, b, False)]):+7.1%} {hit:>7}")


if __name__ == "__main__":
    main()
