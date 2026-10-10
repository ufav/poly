"""Trailing at halftime, then k rounds in a row after the half: when is buying the comeback team worth it?

1. All maps with rounds (no prices): team trailing by d after 12 rounds wins the first k rounds of the second half
   -> how often it wins the map.
2. Same for the pre-match favorite (match price >= --fav) vs Polymarket map price after round 12+k.
   Time of round m = map begin + length * m / rounds (PandaScore); the price is taken --offsets seconds later,
   so the streak is surely in the price (an earlier price would create a fake edge).

  python analysis/comeback_streak.py [--fav 0.60] [--slippage 0.01]
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmbot import storage  # noqa: E402
from halftime_price import collect  # noqa: E402
from streak import maps_with_rounds  # noqa: E402

DEFICITS = (2, 4, 6)
MAX_K = 8


def won_first_k(seq: list[bool], k: int) -> bool:
    return len(seq) >= 12 + k and all(seq[12:12 + k])


def price_after(path: list[tuple[int, float]], t: float) -> float | None:
    return next((p for ts, p in path if ts >= t), None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fav", type=float, default=0.60)
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--offsets", default="60,180")
    ap.add_argument("--side", choices=("fav", "dog"), default="fav",
                    help="fav: buy the favorite on its streak; dog: buy the leading underdog at the same moment")
    args = ap.parse_args()
    offsets = [int(x) for x in args.offsets.split(",")]
    conn = storage.connect()

    print("1. All maps: trailing by d after 12 rounds, won the first k rounds of the 2nd half -> map win")
    table = defaultdict(lambda: [0, 0])
    for winners in maps_with_rounds(conn):
        teams = set(winners[:12])
        if len(teams) != 2:
            continue
        map_winner = max(set(winners), key=winners.count)
        for team in teams:
            seq = [w == team for w in winners]
            d = 12 - 2 * sum(seq[:12])
            if d not in DEFICITS:
                continue
            for k in range(MAX_K + 1):
                if not won_first_k(seq, k):
                    break
                table[(d, k)][0] += map_winner == team
                table[(d, k)][1] += 1
    for d in DEFICITS:
        score = f"{6 - d // 2}:{6 + d // 2}"
        cells = []
        for k in range(MAX_K + 1):
            w, n = table[(d, k)]
            if n >= 15:
                cells.append(f"k={k}: {w / n:4.0%} (n={n})")
        print(f"   half {score} (-{d}) " + " | ".join(cells))

    who = "the favorite" if args.side == "fav" else "the leading underdog"
    print(f"\n2. Pre-match favorite (>= {args.fav:.2f}) trailing at the half, k straight rounds in the 2nd half: buy {who}")
    rows, _ = collect(conn, (60,))
    favs = [r for r in rows if r["pre"] >= args.fav and -r["lead"] in DEFICITS]
    for d in (*DEFICITS, None):
        label = f"-{d}" if d else "all"
        for k in range(0, MAX_K + 1):
            items = [r for r in favs if (d is None or -r["lead"] == d) and won_first_k(r["seq_all"], k)]
            if len(items) < 15:
                continue
            cells = []
            for off in offsets:
                pts = []
                for r in items:
                    n_rounds = len(r["seq_all"])
                    t_round = r["t0"] + (r["t1"] - r["t0"]) * (12 + k) / n_rounds
                    p = price_after(r["path"], t_round + off)
                    if p is not None and p < 0.99:
                        pts.append((p, r["won"]))
                if len(pts) < 15:
                    continue
                if args.side == "dog":
                    pts = [(1 - p, not w) for p, w in pts]
                n = len(pts)
                win = sum(w for _, w in pts) / n
                price = sum(p for p, _ in pts) / n
                cost = sum(p + args.slippage + 0.05 * p * (1 - p) for p, _ in pts) / n
                se2 = 2 * (win * (1 - win) / n) ** 0.5
                cells.append(f"+{off}s n={n:3d} price {price:.2f} win {win:4.0%}±{se2:3.0%} ROI {(win - cost) / cost:+5.0%}")
            if cells:
                print(f"   {label:>3} k={k}  " + "  |  ".join(cells))


if __name__ == "__main__":
    main()
