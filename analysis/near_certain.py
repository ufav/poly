"""Favorite-longshot bias in play: are nearly decided maps underpriced?

Entry = the first round m >= 13 after which the team leads by >= L with >= W rounds won (one entry per map and rule).
Price = map price at estimated end of round m (map begin + length * m / rounds, PandaScore) + offset; entries with
price >= 0.99 are skipped (no room); past the map end the last price is used. A late price only shrinks a real
edge (martingale), an early one could fake it — hence the offsets.

  python analysis/near_certain.py [--slippage 0.01] [--offsets 60,180]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmbot import storage  # noqa: E402
from halftime_price import collect  # noqa: E402

RULES = [(3, 9), (4, 10), (5, 10), (5, 11), (6, 11), (4, 12), (6, 12), (8, 12)]
PRICE_BUCKETS = [(0.80, 0.90), (0.90, 0.95), (0.95, 0.97), (0.97, 0.99)]


def entry(row: dict, lead: int, won_min: int, offset: int) -> tuple[float, int] | None:
    seq = row["seq_all"]
    n = len(seq)
    a = sum(seq[:12])
    b = 12 - a
    for m in range(13, n):
        if seq[m - 1]:
            a += 1
        else:
            b += 1
        if a - b >= lead and a >= won_min:
            t = row["t0"] + (row["t1"] - row["t0"]) * m / n + offset
            # past the map end -> last price (≈ outcome): biases the edge toward 0, never fakes it
            p = next((p for ts, p in row["path"] if ts >= t), row["path"][-1][1] if row["path"] else None)
            return (p, m) if p is not None and p < 0.99 else None
    return None


def line(label: str, pts: list[tuple[float, bool]], slippage: float) -> str:
    n = len(pts)
    win = sum(w for _, w in pts) / n
    price = sum(p for p, _ in pts) / n
    cost = sum(p + slippage + 0.05 * p * (1 - p) for p, _ in pts) / n
    se2 = 2 * (win * (1 - win) / n) ** 0.5
    return (f"   {label:30} n={n:4d}  price {price:.3f}  win {win:6.1%} ±{se2:4.1%}  edge {win - price:+.3f}"
            f"  ROI {(win - cost) / cost:+6.2%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--offsets", default="60,180,300")
    args = ap.parse_args()
    offsets = [int(x) for x in args.offsets.split(",")]
    rows, _ = collect(storage.connect(), (60,))
    print(f"team-map rows: {len(rows)}\n")

    for off in offsets:
        print(f"Entry +{off}s after the estimated end of the round")
        for lead, won_min in RULES:
            pts, meta = [], []
            for r in rows:
                e = entry(r, lead, won_min, off)
                if e:
                    pts.append((e[0], r["won"]))
                    meta.append(r)
            if len(pts) >= 30:
                print(line(f"lead >= {lead}, rounds >= {won_min}", pts, args.slippage))
        print()

    off = offsets[min(1, len(offsets) - 1)]
    lead, won_min = 5, 11
    print(f"Rule lead >= {lead}, rounds >= {won_min}, +{off}s: robustness")
    items = []
    for r in rows:
        e = entry(r, lead, won_min, off)
        if e:
            items.append((e[0], r))
    splits = {
        "all": items,
        "Sep 2025 - Mar 2026": [x for x in items if x[1]["date"] < "2026-04-01"],
        "Apr 2026 - Oct 2026": [x for x in items if x[1]["date"] >= "2026-04-01"],
        "tier1": [x for x in items if x[1]["tier"] == "tier1"],
        "not tier1": [x for x in items if x[1]["tier"] != "tier1"],
        "pre-match favorite": [x for x in items if x[1]["pre"] >= 0.5],
        "pre-match underdog": [x for x in items if x[1]["pre"] < 0.5],
    }
    for lo, hi in PRICE_BUCKETS:
        splits[f"price {lo:.2f}-{hi:.2f}"] = [x for x in items if lo <= x[0] < hi]
    for label, xs in splits.items():
        if len(xs) >= 30:
            print(line(label, [(p, r["won"]) for p, r in xs], args.slippage))


if __name__ == "__main__":
    main()
