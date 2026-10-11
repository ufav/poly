"""Favourite/longshot bias by market size, using only what is known at the trade time (A-40, after R-16).

R-16: in Dota the bias (favourite 0.70-0.92 underpriced, longshot overpriced) sits in markets with final volume
< $200k — but final volume is not known while trading. Segments here:
  - tournament tier: keywords (pmbot.tiers), PandaScore tier s/a/b/c/d, LAN/online;
  - market: match or map;
  - prior $: taker notional in this market in the 30 min before the trade (markets with a cut tape — 10 000 trades
    API limit — skip the first 30 min of their tape);
  - final volume (look-ahead, for comparison);
  - period: before / after the median event date (out-of-sample check).
ROI per trade after the taker fee; error — bootstrap over events (maps and match of one event are correlated).

  python analysis/liquidity_bias.py --sport dota2 [--phase live]
"""

import argparse
import random
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402
from pmbot.tiers import tier_of  # noqa: E402

BANDS = {"fav 0.70-0.92": (0.70, 0.92), "dog 0.08-0.30": (0.08, 0.30)}
WINDOW = 1800
TAPE_LIMIT = 10_000
BOOT = 300


def prior_bucket(usd: float) -> str:
    for hi, label in ((1_000, "a <1k"), (5_000, "b 1-5k"), (20_000, "c 5-20k"), (100_000, "d 20-100k")):
        if usd < hi:
            return label
    return "e >=100k"


def volume_bucket(v: float) -> str:
    for hi, label in ((50_000, "a <50k"), (200_000, "b 50-200k"), (1_000_000, "c 200k-1M")):
        if v < hi:
            return label
    return "d >=1M"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", choices=storage.SPORTS, default="dota2")
    ap.add_argument("--phase", choices=("live", "pre-match"), default="live")
    args = ap.parse_args()
    conn = storage.connect(storage.db_path(args.sport))
    ps = {g: (tier, kind) for g, tier, kind in conn.execute(
        """SELECT i.match_id, t.tier, t.type FROM ps_match_info i JOIN ps_tournaments t ON t.id = i.tournament_id""")}
    markets = conn.execute(
        """SELECT m.condition_id, m.token_a, m.final_price_a, m.market_type, m.volume, e.id, e.start_time,
                  e.tournament, e.game_id
           FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
           WHERE m.trades_status = 'done' AND (m.final_price_a >= 0.99 OR m.final_price_a <= 0.01)""").fetchall()
    starts = sorted(s for *_, s, _, _ in markets if s)
    median = starts[len(starts) // 2][:10]
    print(f"markets {len(markets)}; phase {args.phase}; period split {median}")

    # (segmentation, value, band) -> event -> [n, sum ret]
    agg: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    for cond, tok_a, final_a, mtype, volume, event, start, tournament, game_id in markets:
        try:
            start_ts = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError):
            continue
        trades = conn.execute("SELECT t, asset, side, price, size FROM pm_trades WHERE condition_id = ? ORDER BY t",
                              (cond,)).fetchall()
        if not trades:
            continue
        cut = len(trades) >= TAPE_LIMIT - 10
        times = [t for t, *_ in trades]
        cum = [0.0]
        for _, _, _, price, size in trades:
            cum.append(cum[-1] + price * size)
        tier1 = tier_of(tournament, args.sport) == "tier1"
        ps_tier, ps_type = ps.get(game_id, (None, None))
        fixed = {
            "tier (keywords)": "tier1" if tier1 else "not tier1",
            "PandaScore tier": ps_tier or "?",
            "LAN/online": ps_type or "?",
            "market": "match" if mtype == "moneyline" else "map",
            "final volume": volume_bucket(volume or 0),
            "tier1 x final volume": f"{'tier1' if tier1 else 'other'} {'<200k' if (volume or 0) < 200_000 else '>=200k'}",
            "period": f"{'a before' if start[:10] < median else 'b from'} {median}",
        }
        fixed["LAN/online x period"] = f"{fixed['LAN/online']} | {fixed['period']}"
        fixed["tier x period"] = f"{fixed['tier (keywords)']} | {fixed['period']}"
        a_won = final_a >= 0.99
        for i, (t, asset, side, price, size) in enumerate(trades):
            if (t >= start_ts) != (args.phase == "live"):
                continue
            on_a = (asset == tok_a) == (side == "BUY")
            p = price if side == "BUY" else 1 - price
            band = next((b for b, (lo, hi) in BANDS.items() if lo <= p < hi), None)
            if band is None:
                continue
            ret = (1 if a_won == on_a else 0) / (p + 0.05 * p * (1 - p)) - 1
            values = dict(fixed)
            if not (cut and t - times[0] < WINDOW):
                j0, j1 = bisect_left(times, t - WINDOW), bisect_left(times, t)
                prior = prior_bucket(cum[j1] - cum[j0])
                values["prior $ 30 min"] = prior
                values["prior $ 30 min x period"] = f"{prior} | {fixed['period']}"
                values["tier x prior $"] = f"{fixed['tier (keywords)']} | {prior}"
                if tier1:
                    thin = "thin <5k" if prior < "c" else "busy >=5k"
                    values["tier1: prior $ x period"] = f"{thin} | {fixed['period']}"
            for seg, val in values.items():
                cell = agg[(seg, val, band)][event]
                cell[0] += 1
                cell[1] += ret

    rng = random.Random(1)
    for seg in dict.fromkeys(k[0] for k in agg):
        print(f"\n{seg}")
        print(f"   {'value':32} " + "".join(f"{b:>34}" for b in BANDS))
        for val in sorted({k[1] for k in agg if k[0] == seg}):
            cols = []
            for band in BANDS:
                cells = list(agg.get((seg, val, band), {}).values())
                n = sum(c[0] for c in cells)
                if n < 200 or len(cells) < 20:
                    cols.append(f"{'-':>34}")
                    continue
                est = sum(c[1] for c in cells) / n
                boots = []
                for _ in range(BOOT):
                    pick = [cells[rng.randrange(len(cells))] for _ in cells]
                    boots.append(sum(c[1] for c in pick) / sum(c[0] for c in pick))
                m = sum(boots) / BOOT
                se = (sum((x - m) ** 2 for x in boots) / (BOOT - 1)) ** 0.5
                cols.append(f"{est:+7.1%} ± {se:4.1%} ({len(cells):4d} ev, {n:7d} tr)")
            print(f"   {val:32} " + "".join(f"{c:>34}" for c in cols))


if __name__ == "__main__":
    main()
