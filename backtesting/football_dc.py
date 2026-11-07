"""Walk-forward check of the football Dixon-Coles model (ml_engine/football/dixon_coles.py) on the stored history:
every week from --since the model is refit on the matches before that week only and predicts the week's matches.

Compared with the markets on the same matches:
  - bookmakers: no-vig closing 1x2 of Pinnacle, else the market average (football-data);
  - Polymarket: the last history price before kick-off of the three 1x2 'Yes' tokens, normalized to sum 1.
Metrics: log loss / Brier of the 1x2; calibration of the favourite (the model's most likely outcome) by probability
bucket; flat bets on Polymarket where model p * odds - 1 >= edge, odds = 1 / (price + half spread).
Predictions (rates and rho, so any market can be derived) go to data/football_dc_predictions.csv.

  python -m backtesting.football_dc [--since 2025-08-01] [--xi 0.0019]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

from core.db import engine
from ml_engine.football import dixon_coles as dc

OUT = Path(__file__).resolve().parents[1] / "data" / "football_dc_predictions.csv"
HALF_SPREAD = 0.01    # Polymarket history is the mid price; a buy pays ~1 cent more
EDGES = (0.0, 0.02, 0.05, 0.10)


def walk_forward(since: dt.datetime, until: dt.datetime, xi: float) -> pd.DataFrame:
    rows, t0 = [], time.time()
    week = since
    with engine().connect() as c:
        while week < until:
            nxt = week + dt.timedelta(days=7)
            pool = dc.load_pool(c, nxt, days=dc.WINDOW + 7)
            train = pool[pd.to_datetime(pool["start_time"], utc=True) < week]
            test = pool[pd.to_datetime(pool["start_time"], utc=True) >= week]
            model = dc.fit(train, week, xi=xi)
            for r in test.itertuples():
                rates = model.rates(int(r.h), int(r.a))
                if not rates:
                    continue
                mk = dc.markets(model.matrix(*rates))
                rows.append({"match_id": r.id, "league_id": r.league_id, "start_time": r.start_time,
                             "home_team_id": r.h, "away_team_id": r.a, "hg": r.hg, "ag": r.ag,
                             "mu_home": rates[0], "mu_away": rates[1], "rho": model.rho,
                             "p_home": mk["home"], "p_draw": mk["draw"], "p_away": mk["away"],
                             "p_btts": mk["btts_yes"], "p_over25": dc.p_total_over(mk, 2.5)})
            print(f"  {week:%Y-%m-%d}: trained on {len(train)}, predicted {len(test)}  "
                  f"({(time.time() - t0) / 60:.1f} min)", flush=True)
            week = nxt
    return pd.DataFrame(rows)


def market_probs(match_ids: list[int], chunk: int = 3000) -> pd.DataFrame:
    """Per match: no-vig bookmaker closing 1x2 and the Polymarket pre-kick-off 1x2 (normalized) + raw prices."""
    parts = [_market_rows(match_ids[i:i + chunk]) for i in range(0, len(match_ids), chunk)]
    bk = pd.concat([p[0] for p in parts], ignore_index=True)
    pm = pd.concat([p[1] for p in parts], ignore_index=True)
    return _market_table(match_ids, bk, pm)


def _market_rows(match_ids: list[int]) -> tuple[pd.DataFrame, pd.DataFrame]:
    with engine().connect() as c:
        c.execute(text("SET max_parallel_workers_per_gather = 0"))
        bk = pd.read_sql(text("""
            SELECT mk.match_id, o.bookmaker, o.outcome, o.odds FROM markets mk JOIN odds_history o ON o.market_id = mk.id
            WHERE mk.kind = '1x2' AND o.source_id = 1 AND o.snapshot = 'closing' AND o.odds > 1
              AND o.bookmaker IN ('pinnacle', 'market_avg') AND mk.match_id = ANY(:ids)"""), c, params={"ids": match_ids})
        pm = pd.read_sql(text("""
            SELECT DISTINCT ON (mk.match_id, o.outcome) mk.match_id, o.outcome, o.price
            FROM markets mk JOIN odds_history o ON o.market_id = mk.id JOIN matches m ON m.id = mk.match_id
            WHERE mk.kind = '1x2' AND o.source_id = 4 AND o.snapshot = 'history' AND o.taken_at <= m.start_time
              AND mk.match_id = ANY(:ids)
            ORDER BY mk.match_id, o.outcome, o.taken_at DESC"""), c, params={"ids": match_ids})
    return bk, pm


def _market_table(match_ids: list[int], bk: pd.DataFrame, pm: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({"match_id": match_ids}).set_index("match_id")
    if len(bk):
        piv = bk.pivot_table(index=["match_id", "bookmaker"], columns="outcome", values="odds").dropna()
        inv = 1 / piv[["home", "draw", "away"]]
        nv = inv.div(inv.sum(axis=1), axis=0).reset_index()
        best = nv.sort_values("bookmaker", key=lambda s: s.map({"pinnacle": 0, "market_avg": 1})) \
            .drop_duplicates("match_id").set_index("match_id")
        out = out.join(best[["home", "draw", "away", "bookmaker"]].add_prefix("bk_"))
    if len(pm):
        piv = pm.pivot_table(index="match_id", columns="outcome", values="price").dropna()
        piv = piv[(piv > 0.005).all(axis=1)]
        out = out.join(piv[["home", "draw", "away"]].add_prefix("pm_price_"))
        s = piv[["home", "draw", "away"]].sum(axis=1)
        out = out.join(piv[["home", "draw", "away"]].div(s, axis=0).add_prefix("pm_"))
    return out.reset_index()


def scores(df: pd.DataFrame, cols: dict[str, list[str]]) -> pd.DataFrame:
    y = np.select([df.hg > df.ag, df.hg == df.ag], [0, 1], 2)
    onehot = np.eye(3)[y]
    out = {}
    for name, c in cols.items():
        p = np.clip(df[c].to_numpy(dtype=float), 1e-6, 1)
        out[name] = {"log_loss": float(-np.mean(np.log(p[np.arange(len(y)), y]))),
                     "brier": float(np.mean(np.sum((p - onehot) ** 2, axis=1)))}
    return pd.DataFrame(out).T


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2025-08-01")
    ap.add_argument("--xi", type=float, default=dc.XI)
    args = ap.parse_args()
    since = dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.timezone.utc)
    until = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    pred = walk_forward(since, until, args.xi)
    OUT.parent.mkdir(exist_ok=True)
    pred.to_csv(OUT, index=False)  # kept even if the market queries fail
    pred = pred.merge(market_probs(pred["match_id"].astype(int).tolist()), on="match_id", how="left")
    OUT.parent.mkdir(exist_ok=True)
    pred.to_csv(OUT, index=False)
    print(f"\n{len(pred)} predictions -> {OUT}")

    model = ["p_home", "p_draw", "p_away"]
    bk = ["bk_home", "bk_draw", "bk_away"]
    pm = ["pm_home", "pm_draw", "pm_away"]
    for label, sub, cols in (("vs bookmaker closing", pred.dropna(subset=bk), {"model": model, "bookmaker": bk}),
                             ("vs Polymarket", pred.dropna(subset=pm), {"model": model, "polymarket": pm}),
                             ("all three", pred.dropna(subset=bk + pm),
                              {"model": model, "bookmaker": bk, "polymarket": pm})):
        print(f"\n{label}: {len(sub)} matches")
        print(scores(sub, cols).round(4).to_string())

    # favourite calibration: the model's most likely outcome
    y = np.select([pred.hg > pred.ag, pred.hg == pred.ag], [0, 1], 2)
    p = pred[model].to_numpy()
    fav = p.argmax(axis=1)
    pf = p.max(axis=1)
    cal = pd.DataFrame({"p": pf, "won": (fav == y).astype(int)})
    cal["bucket"] = pd.cut(cal.p, [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
    print("\nfavourite calibration (model p of its most likely outcome vs how often it happened):")
    print(cal.groupby("bucket", observed=True).agg(n=("won", "size"), model_p=("p", "mean"), won=("won", "mean"))
          .round(3).to_string())

    # flat bets on Polymarket outcomes the model rates above the price
    sub = pred.dropna(subset=["pm_price_home", "pm_price_draw", "pm_price_away"])
    bets = []
    for k, o in enumerate(("home", "draw", "away")):
        price = sub[f"pm_price_{o}"] + HALF_SPREAD
        bets.append(pd.DataFrame({"p": sub[model[k]], "odds": 1 / price,
                                  "won": (np.select([sub.hg > sub.ag, sub.hg == sub.ag], [0, 1], 2) == k)}))
    bets = pd.concat(bets)
    bets["ev"] = bets.p * bets.odds - 1
    print(f"\nflat bets at the Polymarket price (+{HALF_SPREAD} spread), {len(sub)} matches:")
    for e in EDGES:
        b = bets[bets.ev >= e]
        roi = (b.won * b.odds - 1).mean() if len(b) else float("nan")
        print(f"  edge >= {e:.2f}: {len(b):5d} bets, won {b.won.mean() if len(b) else 0:.3f}, "
              f"mean odds {b.odds.mean() if len(b) else 0:.2f}, ROI {roi:+.3f}")


if __name__ == "__main__":
    main()
