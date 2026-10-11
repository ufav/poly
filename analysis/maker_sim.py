"""Maker simulation on the taker tape (A-27): a resting bid on the favourite, re-quoted after every trade.

No historical order books, so the quote follows the tape: reference = last traded price (implied for token A from
trades strictly before the current second, not older than --max-age s); the side priced in [--lo, --hi] is the
favourite F; our bid on F = reference - delta, floored to the cent. A taker selling F (selling F or buying the
other token) fills us:
  - strict: at a price below our bid (traded through our level, so the whole level was taken whatever the queue);
  - touch:  at a price <= our bid (we may have been behind in the queue - optimistic).
Each fill = one flat-stake bet on F at our bid, no fee (makers pay none). Error: bootstrap over events (map and
match markets of one match share outcomes). Also reported: $ we could have filled per event with an order of
--size $ (capped by the taker trade), and an early / late split by event start.

  python analysis/maker_sim.py [--lo 0.70 --hi 0.92 --max-age 60 --size 50] [--sport dota2]
"""

import argparse
import math
import random
import sys
from collections import defaultdict
from datetime import datetime
from itertools import groupby
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmbot import storage  # noqa: E402
from pmbot.tiers import tier_of  # noqa: E402

DELTAS = (0.0, 0.01, 0.02)
RULES = ("strict", "touch")
BOOT = 300


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lo", type=float, default=0.70)
    ap.add_argument("--hi", type=float, default=0.92)
    ap.add_argument("--max-age", type=int, default=60, help="reference trade not older than this, s")
    ap.add_argument("--size", type=float, default=50.0, help="our order size, $")
    ap.add_argument("--sport", choices=storage.SPORTS, default="cs2")
    args = ap.parse_args()
    conn = storage.connect(storage.db_path(args.sport))
    markets = conn.execute(
        """SELECT m.condition_id, m.token_a, m.final_price_a, e.start_time, e.tournament, e.id
           FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
           WHERE m.trades_status = 'done' AND (m.final_price_a >= 0.99 OR m.final_price_a <= 0.01)
           ORDER BY e.start_time""").fetchall()
    split_start = markets[len(markets) // 2][3]

    # (segment, delta, rule) -> event (maps of one match share outcomes) -> [fills, sum return, sum bid, wins, $]
    agg: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0.0, 0, 0.0]))
    for cond, tok_a, final_a, start, tournament, event_id in markets:
        try:
            start_ts = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError):
            continue
        a_won = final_a >= 0.99
        tier = "tier1" if tier_of(tournament, args.sport) == "tier1" else "not tier1"
        segs = ("all", tier, f"{tier} {'early' if start < split_start else 'late'}")
        ref, ref_t = None, None
        rows = conn.execute("SELECT t, asset, side, price, size FROM pm_trades WHERE condition_id = ? ORDER BY t",
                            (cond,))
        for t, group in groupby(rows, key=lambda r: r[0]):
            group = list(group)
            if t >= start_ts and ref is not None and t - ref_t <= args.max_age:
                if args.lo <= ref <= args.hi:
                    fav_a, fav_ref = True, ref
                elif args.lo <= 1 - ref <= args.hi:
                    fav_a, fav_ref = False, 1 - ref
                else:
                    fav_a = None
                if fav_a is not None:
                    for _, asset, side, price, size in group:
                        on_a = asset == tok_a
                        p_a = price if on_a else 1 - price
                        taker_buys_a = (side == "BUY") == on_a
                        if taker_buys_a == fav_a:  # taker buys F: not our side
                            continue
                        p_fav = p_a if fav_a else 1 - p_a
                        won = a_won == fav_a
                        for delta in DELTAS:
                            bid = math.floor((fav_ref - delta) * 100 + 1e-9) / 100
                            for rule in RULES:
                                if p_fav < bid or (rule == "touch" and p_fav <= bid + 1e-9):
                                    ret = (1 if won else 0) / bid - 1
                                    usd = min(args.size, size * bid)
                                    for seg in segs:
                                        cell = agg[(seg, delta, rule)][event_id]
                                        cell[0] += 1
                                        cell[1] += ret
                                        cell[2] += bid
                                        cell[3] += won
                                        cell[4] += usd
            last = group[-1]
            ref = last[3] if last[1] == tok_a else 1 - last[3]
            ref_t = t

    rng = random.Random(1)
    print(f"markets: {len(markets)}; live only; favourite band {args.lo}-{args.hi}; reference age <= {args.max_age} s; "
          f"order ${args.size:.0f}; early/late split at {split_start[:10]}\n")
    print(f"   {'segment':15} {'delta':>5} {'rule':6} {'fills':>7} {'events':>6} {'avg bid':>7} {'win':>6} "
          f"{'ROI/fill':>15} {'$/event':>8}")
    for seg in ("all", "tier1", "tier1 early", "tier1 late", "not tier1", "not tier1 early", "not tier1 late"):
        for delta in DELTAS:
            for rule in RULES:
                cells = list(agg.get((seg, delta, rule), {}).values())
                n = sum(c[0] for c in cells)
                if n < 50:
                    continue
                roi = sum(c[1] for c in cells) / n
                boots = []
                for _ in range(BOOT):
                    pick = [cells[rng.randrange(len(cells))] for _ in cells]
                    boots.append(sum(c[1] for c in pick) / sum(c[0] for c in pick))
                m = sum(boots) / BOOT
                se = (sum((x - m) ** 2 for x in boots) / (BOOT - 1)) ** 0.5
                print(f"   {seg:15} {delta:5.2f} {rule:6} {n:7d} {len(cells):6d} {sum(c[2] for c in cells) / n:7.3f} "
                      f"{sum(c[3] for c in cells) / n:6.1%} {roi:+7.1%} ±{se:5.1%} "
                      f"{sum(c[4] for c in cells) / len(cells):8.0f}")
        print()


if __name__ == "__main__":
    main()
