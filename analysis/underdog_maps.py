"""BO3: underdog by match price at scheduled start (odds in [--lo, --hi)) — how often it wins map 1, map 2, map 3
(map 3 is played only at 1:1), plus map 2 after winning / losing map 1. Map winners: between_maps.load_bo3.

  python analysis/underdog_maps.py [--sport dota2] [--lo 2.0] [--hi 2.5]
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmbot import storage  # noqa: E402
from pmbot.tiers import tier_of  # noqa: E402
from between_maps import load_bo3, price_point  # noqa: E402


def rate(items: list[bool]) -> str:
    n = len(items)
    if not n:
        return "   -"
    w = sum(items)
    se = (w / n * (1 - w / n) / n) ** 0.5
    return f"{w / n:6.1%} ±{se:4.1%} (n {n})"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="dota2", choices=storage.SPORTS)
    ap.add_argument("--lo", type=float, default=2.0)
    ap.add_argument("--hi", type=float, default=2.5)
    args = ap.parse_args()

    conn = storage.connect(storage.db_path(args.sport))
    groups = {"tier1": [], "tier2+": []}
    for m in load_bo3(conn):
        start = int(dt.datetime.fromisoformat(m["start"].replace("Z", "+00:00")).timestamp())
        pp = price_point(conn, m["ml_token"], start)
        if not pp or not 0.01 < pp[0] < 0.99:
            continue
        p_a = pp[0]
        dog, p = ("A", p_a) if p_a <= 0.5 else ("B", 1 - p_a)
        if not args.lo <= 1 / p < args.hi:
            continue
        tier = "tier1" if tier_of(m["tournament"], args.sport) == "tier1" else "tier2+"
        groups[tier].append({"p": p, "won": [w == dog for w in m["winners"]], "match": m["match_winner"] == dog})

    print(f"{args.sport} BO3, underdog = match odds {args.lo}-{args.hi} at scheduled start "
          f"(win rate ± standard error)\n")
    for tier, rows in list(groups.items()) + [("all", groups["tier1"] + groups["tier2+"])]:
        if not rows:
            continue
        avg_p = sum(r["p"] for r in rows) / len(rows)
        print(f"{tier}: {len(rows)} matches, avg match price {avg_p:.3f} (odds {1 / avg_p:.2f})")
        print(f"  match              {rate([r['match'] for r in rows])}")
        print(f"  map 1              {rate([r['won'][0] for r in rows])}")
        print(f"  map 2              {rate([r['won'][1] for r in rows])}")
        print(f"    after won map 1  {rate([r['won'][1] for r in rows if r['won'][0]])}")
        print(f"    after lost map 1 {rate([r['won'][1] for r in rows if not r['won'][0]])}")
        print(f"  map 3 (at 1:1)     {rate([r['won'][2] for r in rows if len(r['won']) == 3])}")
        print(f"  score 2:0 {sum(r['won'][:2] == [True, True] for r in rows) / len(rows):.1%}  "
              f"2:1 {sum(len(r['won']) == 3 and r['match'] for r in rows) / len(rows):.1%}  "
              f"1:2 {sum(len(r['won']) == 3 and not r['match'] for r in rows) / len(rows):.1%}  "
              f"0:2 {sum(r['won'][:2] == [False, False] for r in rows) / len(rows):.1%}\n")


if __name__ == "__main__":
    main()
