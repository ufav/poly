"""Look at a copy of the server live.sqlite: paper summary, trades, score coverage, order book quality.

  python scripts/research/server_live_check.py [data/server_live.sqlite]
"""

import datetime as dt
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot.live import report, store  # noqa: E402


def ts(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%m-%d %H:%M")


def depth(levels: list, limit: float, side: str) -> float:
    """$ available within `limit` of the best price."""
    if not levels:
        return 0.0
    best = float(levels[0][0] if isinstance(levels[0], list) else levels[0]["price"])
    total = 0.0
    for lv in levels:
        p, s = (float(lv[0]), float(lv[1])) if isinstance(lv, list) else (float(lv["price"]), float(lv["size"]))
        if (side == "ask" and p > best + limit) or (side == "bid" and p < best - limit):
            break
        total += p * s
    return total


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/server_live.sqlite")
    conn = store.connect(path)
    print(report.summary(conn), "\n")

    first, last = conn.execute("SELECT MIN(t), MAX(t) FROM live_scores").fetchone()
    print(f"scores from {ts(first)} to {ts(last)} UTC")
    print("games:", conn.execute("SELECT COUNT(*), SUM(ended_at IS NOT NULL) FROM live_games").fetchone(),
          "| with scores:", conn.execute("SELECT COUNT(DISTINCT game_id) FROM live_scores").fetchone()[0],
          "| book snapshots:", conn.execute("SELECT COUNT(*), COUNT(DISTINCT token_id) FROM live_books").fetchone())

    print("\npaper trades:")
    for row in conn.execute(
            """SELECT p.signal_t, p.strategy, g.title, p.map_number, p.outcome, p.state, p.avg_price, p.best_ask,
                      p.filled, p.status, p.pnl FROM paper_trades p LEFT JOIN live_games g ON g.game_id = p.game_id
               ORDER BY p.signal_t"""):
        t, strat, title, m, outcome, state, price, ask, filled, status, pnl = row
        print(f"  {ts(t)} {strat:18} {title} | map {m} | {outcome} @ {price or 0:.3f} (ask {ask or 0:.3f}, "
              f"filled {(filled or 0):.0%}) | {state} | {status} {'' if pnl is None else f'{pnl:+.2f}$'}")

    # book quality per map market token: spread and $ within 2 c of the ask, during live play
    tok_info = {}
    for mtype, m, ta, tb, title in conn.execute(
            """SELECT lm.market_type, lm.map_number, lm.token_a, lm.token_b, g.title FROM live_markets lm
               JOIN live_games g ON g.event_id = lm.event_id"""):
        tok_info[ta] = tok_info[tb] = (mtype, title)
    stats = defaultdict(list)
    for token, bid, ask, levels in conn.execute("SELECT token_id, bid, ask, levels FROM live_books"):
        if bid is None or ask is None:
            continue
        lv = json.loads(levels or "{}")
        mid = (bid + ask) / 2
        stats[token].append((ask - bid, depth(lv.get("asks", []), 0.02, "ask"), mid))
    print(f"\nbooks: {len(stats)} tokens")
    spreads, depths = [], []
    for token, xs in stats.items():
        live = [x for x in xs if 0.05 < x[2] < 0.95]
        if not live:
            continue
        spreads.append(statistics.median(x[0] for x in live))
        depths.append(statistics.median(x[1] for x in live))
    if spreads:
        qs = lambda v: [round(statistics.quantiles(v, n=4)[i], 3) for i in range(3)]  # noqa: E731
        print(f"  per-token median spread quartiles {qs(spreads)}; $ within 2c of ask quartiles {qs(depths)}")
        print(f"  share of tokens with median spread <= 0.03: {sum(s <= 0.03 for s in spreads) / len(spreads):.0%}; "
              f"with >= $100 near ask: {sum(d >= 100 for d in depths) / len(depths):.0%}")


if __name__ == "__main__":
    main()
