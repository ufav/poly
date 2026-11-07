"""Are favourites priced right on Polymarket? For every outcome of a past game with a price history (moneyline,
football 1x2, totals, spreads, tennis total games) the last price before kick-off vs whether it won: win rate and
ROI by price bucket (buy at price + half spread), by sport and market; only outcomes whose price moved in the
last day (the history of a dead market is the mid of an empty book). A favourite-longshot bias (favourites winning
more often than their price) would make 'the most likely outcomes, paired to odds ~2' profitable without a model.

  python analysis/pm_favourites.py [--spread 0.01]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import engine  # noqa: E402

BUCKETS = [0.0, 0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0]


def load() -> pd.DataFrame:
    with engine().connect() as c:
        c.execute(text("SET max_parallel_workers_per_gather = 0"))
        return pd.read_sql(text("""
            SELECT s.code sport, mk.kind, mk.line, o.outcome, o.price, o.moves, m.id match_id, m.start_time,
                   m.home_score hs, m.away_score aws, m.periods, m.result_extra
            FROM (SELECT DISTINCT ON (o.market_id, o.outcome) o.market_id, o.outcome, o.price,
                         count(*) FILTER (WHERE o.taken_at > m.start_time - interval '24 hours')
                           OVER (PARTITION BY o.market_id, o.outcome) moves
                  FROM odds_history o JOIN markets mk ON mk.id = o.market_id JOIN matches m ON m.id = mk.match_id
                  WHERE o.source_id = 4 AND o.snapshot = 'history' AND o.taken_at <= m.start_time
                  ORDER BY o.market_id, o.outcome, o.taken_at DESC) o
            JOIN markets mk ON mk.id = o.market_id JOIN matches m ON m.id = mk.match_id
            JOIN sports s ON s.id = m.sport_id
            WHERE m.status = 'finished' AND m.home_score IS NOT NULL
              AND mk.kind IN ('moneyline', '1x2', 'total', 'spread', 'total_games')"""), c)


def resolve(df: pd.DataFrame) -> pd.Series:
    """1 won, 0 lost, NaN push / unknown."""
    hs, aws = df.hs.astype(float), df.aws.astype(float)
    line = df.line.astype(float)
    won = pd.Series(np.nan, index=df.index)
    ml = df.kind.isin(["moneyline", "1x2"])
    won[ml & (df.outcome == "home")] = (hs > aws).astype(float)
    won[ml & (df.outcome == "away")] = (aws > hs).astype(float)
    won[ml & (df.outcome == "draw")] = (hs == aws).astype(float)
    games = df.periods.map(lambda p: sum(a + b for a, b in p) if isinstance(p, list) and p else np.nan)
    tot = np.where(df.kind == "total_games", games, hs + aws)
    for kind_mask, value in ((df.kind.isin(["total", "total_games"]), tot), (df.kind == "spread", hs - aws)):
        m = kind_mask
        if df.kind[m].empty:
            continue
        over = np.where(df.kind == "spread", value + line > 0, value > line)
        push = np.where(df.kind == "spread", value + line == 0, value == line)
        side_home = df.outcome.isin(["over", "home"])
        w = np.where(side_home, over, ~over & ~push).astype(float)
        w[push] = np.nan
        won[m] = w[m]
    incomplete = df.result_extra.map(lambda x: isinstance(x, dict) and bool(x.get("incomplete")))
    won[incomplete & (df.sport == "tennis")] = np.nan  # retirements: settlement rules vary
    return won


def table(df: pd.DataFrame, spread: float) -> pd.DataFrame:
    df = df.assign(bucket=pd.cut(df.price, BUCKETS, right=False))
    g = df.groupby("bucket", observed=True)
    out = g.agg(n=("won", "size"), price=("price", "mean"), won=("won", "mean"))
    out["edge"] = out.won - out.price
    out["roi"] = g.apply(lambda x: (x.won / (x.price + spread)).mean() - 1, include_groups=False)
    return out.round(3)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--spread", type=float, default=0.01)
    ap.add_argument("--min-moves", type=int, default=5,
                    help="price changes in the last 24 h before kick-off: a market that never moved shows the mid "
                         "of an empty book (0.35 / 1.00 -> 0.675), not a price anyone traded")
    args = ap.parse_args()
    df = load()
    df["won"] = resolve(df)
    df = df.dropna(subset=["won"])
    n0 = len(df)
    df = df[df.moves >= args.min_moves]
    print(f"liquid outcomes (>= {args.min_moves} price changes in the last 24 h): {len(df)} of {n0}")
    df = df[(df.price > 0.02) & (df.price < 0.98)]
    print(f"{len(df)} priced outcomes of {df.match_id.nunique()} finished games\n")
    print("all sports, all markets:")
    print(table(df, args.spread).to_string())
    for sport in sorted(df.sport.unique()):
        sub = df[df.sport == sport]
        print(f"\n{sport} ({sub.match_id.nunique()} games), favourites only (price >= 0.5):")
        print(table(sub[sub.price >= 0.5], args.spread).to_string())
    fav = df[df.price >= 0.5]
    print("\nfavourites (price >= 0.5) by market kind:")
    for kind in sorted(fav.kind.unique()):
        sub = fav[fav.kind == kind]
        roi = (sub.won / (sub.price + args.spread)).mean() - 1
        print(f"  {kind:<12} n {len(sub):6d}  price {sub.price.mean():.3f}  won {sub.won.mean():.3f}  ROI {roi:+.3f}")


if __name__ == "__main__":
    main()
