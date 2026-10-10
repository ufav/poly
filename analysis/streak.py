"""Momentum ("кураж"): does a streak of won rounds matter beyond the score, and does the market price it?

1. Next round after a streak of k won rounds (inside a half; economy effect) — all maps with rounds.
2. Map win at halftime by lead x streak at the end of the first half (economy resets at the half,
   so a remaining effect is not money) — all maps with rounds.
3. Same groups vs Polymarket map price at halftime (true map timing, PandaScore): edge and ROI
   (taker fee + --slippage), entry +60 s and +300 s after the estimated half.

  python analysis/streak.py [--slippage 0.01]
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmbot import storage  # noqa: E402
from halftime_price import collect  # noqa: E402


def streak_end(seq: list[bool]) -> int:
    """+k: the team won the last k rounds; -k: lost the last k."""
    if not seq:
        return 0
    last, k = seq[-1], 0
    for won in reversed(seq):
        if won != last:
            break
        k += 1
    return k if last else -k


def bucket(s: int) -> str:
    if s >= 3:
        return "won last 3+"
    if s >= 1:
        return "won last 1-2"
    if s <= -3:
        return "lost last 3+"
    return "lost last 1-2"


BUCKETS = ("won last 3+", "won last 1-2", "lost last 1-2", "lost last 3+")


def maps_with_rounds(conn) -> list[list[str]]:
    """Winner team per round, rounds 1..N, for every map with complete round data."""
    out, cur, seq = [], None, []
    for game_id, rnd, winner in conn.execute(
            "SELECT game_id, round, winner_team FROM cs_rounds ORDER BY game_id, round"):
        if game_id != cur:
            if seq:
                out.append(seq)
            cur, seq = game_id, []
        if rnd != len(seq) + 1:
            seq = None
            cur = game_id
            continue
        if seq is not None:
            seq.append(winner)
    if seq:
        out.append(seq)
    return [s for s in out if s and len(s) >= 13]


def pct(won: int, n: int) -> str:
    p = won / n
    return f"{p:6.1%} ±{2 * (p * (1 - p) / n) ** 0.5:5.1%}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slippage", type=float, default=0.01)
    args = ap.parse_args()
    conn = storage.connect()
    maps = maps_with_rounds(conn)
    print(f"maps with full rounds: {len(maps)}\n")

    print("1. Next round won by the team on a streak (rounds 2-12 and 14-24, same half; pistols excluded)")
    nxt = defaultdict(lambda: [0, 0])
    for seq in maps:
        for i in range(1, len(seq) - 1):
            rnd = i + 2  # number of the next round
            if rnd in (1, 13) or rnd > 24:
                continue
            half_start = 0 if rnd <= 12 else 12
            hist = seq[half_start:i + 1]
            team = seq[i]
            k = 0
            for w in reversed(hist):
                if w != team:
                    break
                k += 1
            k = min(k, 5)
            nxt[k][0] += seq[i + 1] == team
            nxt[k][1] += 1
    for k in sorted(nxt):
        w, n = nxt[k]
        print(f"   streak {k}{'+' if k == 5 else ' '}  n={n:6d}  next round won {pct(w, n)}")

    print("\n2. Map win by halftime lead x the leader's streak at the end of the half (no prices)")
    groups = defaultdict(lambda: [0, 0])
    for seq in maps:
        teams = set(seq[:12])
        if len(teams) != 2:
            continue
        winner = max(set(seq), key=seq.count)
        for team in teams:
            half = [w == team for w in seq[:12]]
            lead = 2 * sum(half) - 12
            if lead < 0 or (lead == 0 and not half[-1]):
                continue
            key = (min(lead, 8), bucket(streak_end(half)))
            groups[key][0] += winner == team
            groups[key][1] += 1
    for lead in (0, 2, 4, 6, 8):
        cells = [f"{b}: n={groups[(lead, b)][1]:4d} {pct(*groups[(lead, b)])}"
                 for b in BUCKETS if groups[(lead, b)][1] >= 30]
        print(f"   lead {'+' if lead else ' '}{lead}{'+' if lead == 8 else ' '} " + " | ".join(cells))
    print("   (lead 0 = 6:6, the team that won round 12)")

    print("\n3. Market at halftime (PandaScore timing): price vs win by lead x streak")
    rows, _ = collect(conn, (60, 300))
    for delay in (60, 300):
        print(f"   entry +{delay}s")
        for lead in (2, 4, 6):
            for b in BUCKETS:
                items = [r for r in rows if r["lead"] == lead and bucket(streak_end(r["seq"])) == b]
                if len(items) < 30:
                    continue
                n = len(items)
                win = sum(r["won"] for r in items) / n
                price = sum(r["price"][delay] for r in items) / n
                cost = sum(r["price"][delay] + args.slippage + 0.05 * r["price"][delay] * (1 - r["price"][delay])
                           for r in items) / n
                print(f"     lead +{lead} {b:14} n={n:4d}  price {price:5.3f}  win {pct(sum(r['won'] for r in items), n)}"
                      f"  edge {win - price:+.3f}  ROI {(win - cost) / cost:+6.1%}")


if __name__ == "__main__":
    main()
