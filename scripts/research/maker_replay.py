"""Replay the paper maker (pmbot.live.paper.maker_fills) on historical tier1 tape, polled like the bot does.

Every --interval s of live play the bot sees the newest 500 taker trades (one Data API page); fills and ROI are
compared with analysis/maker_sim.py (strict, delta 0), which should be close.

  python scripts/research/maker_replay.py [--markets 300 --interval 10]
"""

import argparse
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot import storage  # noqa: E402
from pmbot.live import paper  # noqa: E402
from pmbot.tiers import tier_of  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", type=int, default=300)
    ap.add_argument("--interval", type=int, default=10)
    args = ap.parse_args()
    conn = storage.connect()
    rows = [r for r in conn.execute(
        """SELECT m.condition_id, m.token_a, m.final_price_a, e.start_time, e.tournament
           FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
           WHERE m.trades_status = 'done' AND (m.final_price_a >= 0.99 OR m.final_price_a <= 0.01)""")
        if tier_of(r[4]) == "tier1"]
    random.Random(1).shuffle(rows)
    lo, hi = paper.MAKER_RULES["maker_t1"]
    fills, rets, usd, direct = 0, [], 0.0, []
    for cond, tok_a, final_a, start, _ in rows[:args.markets]:
        start_ts = int(datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp())
        tape = [{"timestamp": t, "asset": a, "side": s, "price": p, "size": z, "transactionHash": tx}
                for t, a, s, p, z, tx in conn.execute(
                    "SELECT t, asset, side, price, size, tx FROM pm_trades WHERE condition_id = ? ORDER BY t", (cond,))]
        live = [x for x in tape if x["timestamp"] >= start_ts]
        if not live:
            continue
        # direct: the whole tape at once, second by second (= maker_sim strict, delta 0)
        qd = paper.MakerQuote()
        paper.maker_fills(qd, [x for x in tape if x["timestamp"] < start_ts], tok_a, lo, hi)
        qd.primed = True
        for fav_a, bid, _ in paper.maker_fills(qd, live, tok_a, lo, hi):
            direct.append((1 if (final_a >= 0.99) == fav_a else 0) / bid - 1)
        q = paper.MakerQuote()
        i, now = 0, start_ts
        while i < len(tape) and now <= tape[-1]["timestamp"] + args.interval:
            while i < len(tape) and tape[i]["timestamp"] <= now:
                i += 1
            page = tape[max(0, i - 500):i]
            for fav_a, bid, notional in paper.maker_fills(q, page, tok_a, lo, hi):
                won = (final_a >= 0.99) == fav_a
                fills += 1
                rets.append((1 if won else 0) / bid - 1)
                usd += min(50, notional)
            now += args.interval
    k = len(rets)
    print(f"tier1 markets replayed: {min(args.markets, len(rows))}; polled every {args.interval} s: fills {fills}, "
          f"ROI/fill {sum(rets) / max(k, 1):+.2%}, $ with $50 orders per market {usd / min(args.markets, len(rows)):.0f}")
    print(f"same markets, whole tape at once: fills {len(direct)}, ROI/fill {sum(direct) / max(len(direct), 1):+.2%}")


if __name__ == "__main__":
    main()
