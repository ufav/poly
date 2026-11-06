"""CS2: the match underdog starts a map well (lead >= 2 in the first 6 rounds), the favourite turns it around and the
underdog's map odds drift to >= K; buy the underdog at that price and sell into a recovery (x1.5 / x2 / x3 of the
stake price), at the half, or hold. Control: the same entry without the early lead. Timing as R-8 (PandaScore map
window, rounds spread evenly); entry/exit on the Polymarket minute history (not ask), fee 0.05*p*(1-p) and slippage
on both sides. Example: Nemiga - Vitality 30.10.2026 map 2 (2-0, then odds ~5, sold ~x3).

  python analysis/dog_bounce.py [--slippage 0.01] [--list]
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmbot import storage  # noqa: E402
from pmbot.maptiming import price_series  # noqa: E402
from halftime_price import collect  # noqa: E402

ODDS_MIN = (3.0, 4.0, 5.0, 6.0)     # entry when the map odds first reach this
DOG_MAX_PRE = 0.45                  # match prematch of the underdog (odds >= 2.22)
EARLY_ROUNDS, EARLY_LEAD = 6, 2
LAST_ENTRY_ROUND = 18               # no entry after the estimated end of this round
MULTS = (1.5, 2.0, 3.0)


def fee(p: float) -> float:
    return 0.05 * p * (1 - p)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--list", action="store_true", help="print the K=5 entries with an early lead")
    args = ap.parse_args()
    slip = args.slippage

    def sell(p: float) -> float:
        p = max(p - slip, 0.0)
        return p - fee(p)

    conn = storage.connect()
    rows, _ = collect(conn, (0,))
    series_cache: dict[str, list] = {}
    # (early lead?, K, scope) -> [(cost, {exit: proceeds}, max multiple reached)]
    data = defaultdict(list)
    listing = []
    for r in rows:
        if r["pre"] > DOG_MAX_PRE:
            continue
        seq, n = r["seq_all"], len(r["seq_all"])
        score, early = [0, 0], None
        for k, won_round in enumerate(seq[:EARLY_ROUNDS], 1):
            score[0 if won_round else 1] += 1
            if early is None and score[0] - score[1] >= EARLY_LEAD:
                early = k
        t_round = lambda k: r["t0"] + (r["t1"] - r["t0"]) * k / n  # noqa: E731
        t_from = t_round(early) + 60 if early else r["t0"]
        t_last = t_round(LAST_ENTRY_ROUND)
        if r["token"] not in series_cache:
            series_cache[r["token"]] = price_series(conn, r["token"])
        series = [(t, p) for t, p in series_cache[r["token"]] if r["t0"] <= t <= r["t1"]]
        scopes = ("all", "tier1") if r["tier"] == "tier1" else ("all",)
        for k_odds in ODDS_MIN:
            entry = next(((t, p) for t, p in series if t_from <= t <= t_last and 0.01 < p <= 1 / k_odds), None)
            if not entry:
                continue
            t_in, p_in = entry
            cost = p_in + slip + fee(p_in + slip)
            path = [p for t, p in series if t > t_in]
            settle = 1.0 if r["won"] else 0.0
            res = {"hold": settle}
            for m in MULTS:
                hit = next((p for p in path if sell(p) >= m * cost), None)
                res[f"x{m:g}"] = sell(hit) if hit is not None else settle
            half = next((p for t, p in series if t >= r["t_half"] and t > t_in), None)
            res["half"] = sell(half) if half is not None and t_in < r["t_half"] else settle
            best = max((sell(p) for p in path), default=0.0) / cost
            for sc in scopes:
                data[(early is not None, k_odds, sc)].append((cost, res, best))
            if args.list and early and k_odds == 5.0:
                listing.append((r["date"], r["team"], r["tier"], 1 / r["pre"], 1 / p_in, best, r["won"]))

    if listing:
        print("early lead, odds >= 5 entries: date, underdog, tier, prematch odds, entry odds, best multiple, map")
        for e in sorted(listing):
            print(f"  {e[0]} {e[1]:<20} {e[2]:<16} {e[3]:5.2f} {e[4]:5.2f} x{e[5]:4.1f} {'won' if e[6] else 'lost'}")
    print(f"\nunderdog = match prematch odds >= {1 / DOG_MAX_PRE:.2f}; early lead = +{EARLY_LEAD} within "
          f"{EARLY_ROUNDS} rounds; entry before round {LAST_ENTRY_ROUND}; slippage {slip}")
    exits = ["hold", *(f"x{m:g}" for m in MULTS), "half"]
    for sc in ("all", "tier1"):
        print(f"\n{sc}")
        print(f"  {'start':<11} {'odds>=':>6} {'n':>5} {'won':>6} " + " ".join(f"{e:>7}" for e in exits)
              + "  " + " ".join(f"{'hit x' + format(m, 'g'):>7}" for m in MULTS))
        for early in (True, False):
            for k_odds in ODDS_MIN:
                items = data[(early, k_odds, sc)]
                if len(items) < 10:
                    continue
                cost = sum(c for c, _, _ in items)
                won = sum(1 for _, x, _ in items if x["hold"] == 1.0) / len(items)
                rois = " ".join(f"{sum(x[e] for _, x, _ in items) / cost - 1:+7.1%}" for e in exits)
                hits = " ".join(f"{sum(1 for _, _, b in items if b >= m) / len(items):7.0%}" for m in MULTS)
                print(f"  {'led early' if early else 'any':<11} {k_odds:6.1f} {len(items):5d} {won:6.1%} {rois}  {hits}")


if __name__ == "__main__":
    main()
