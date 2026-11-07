"""Walk-forward check of the tennis model (ml_engine/tennis/features.py + CatBoost), refit every month from --since
on everything before that month, vs the markets on the same matches:
  - bookmakers: no-vig closing moneyline of tennis-data (Pinnacle, else the market average, else Bet365);
  - Polymarket: the last history price before the start, home / (home + away).
Metrics: log loss / Brier, the market blend weight, favourite calibration, value singles at the Polymarket price,
how often the confident favourites win. Baselines: the plain Elo and the surface-blended Elo.

  python -m backtesting.tennis_cb [--since 2025-10-01] [--train-from 2024-07-01]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sqlalchemy import text

from core.db import engine
from ml_engine.tennis import features as ft

OUT = Path(__file__).resolve().parents[1] / "data" / "tennis_cb_predictions.csv"
HALF_SPREAD = 0.01
CAT = ["surface", "level"]


def market_probs(ids: list[int], chunk: int = 4000) -> pd.DataFrame:
    bks, pms = [], []
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        with engine().connect() as c:
            c.execute(text("SET max_parallel_workers_per_gather = 0"))
            bks.append(pd.read_sql(text("""
                SELECT mk.match_id, o.bookmaker, o.outcome, o.odds FROM markets mk
                JOIN odds_history o ON o.market_id = mk.id
                WHERE mk.kind = 'moneyline' AND o.source_id = 5 AND o.odds > 1 AND mk.match_id = ANY(:ids)"""),
                c, params={"ids": part}))
            pms.append(pd.read_sql(text("""
                SELECT DISTINCT ON (mk.match_id, o.outcome) mk.match_id, o.outcome, o.price
                FROM markets mk JOIN odds_history o ON o.market_id = mk.id JOIN matches m ON m.id = mk.match_id
                WHERE mk.kind = 'moneyline' AND o.source_id = 4 AND o.snapshot = 'history'
                  AND o.taken_at <= m.start_time AND mk.match_id = ANY(:ids)
                ORDER BY mk.match_id, o.outcome, o.taken_at DESC"""), c, params={"ids": part}))
    bk, pm = pd.concat(bks), pd.concat(pms)
    out = pd.DataFrame({"match_id": ids}).set_index("match_id")
    if len(bk):
        piv = bk.pivot_table(index=["match_id", "bookmaker"], columns="outcome", values="odds").dropna()
        piv["bk_home"] = (1 / piv["home"]) / (1 / piv["home"] + 1 / piv["away"])
        order = {"pinnacle": 0, "market_avg": 1, "bet365": 2, "market_max": 3, "betfair_ex": 4}
        best = piv.reset_index().sort_values("bookmaker", key=lambda x: x.map(order)) \
            .drop_duplicates("match_id").set_index("match_id")
        out = out.join(best[["bk_home"]])
    if len(pm):
        piv = pm.pivot_table(index="match_id", columns="outcome", values="price").dropna()
        piv = piv[(piv.home > 0.005) & (piv.away > 0.005)]
        out = out.join(piv[["home", "away"]].rename(columns={"home": "pm_price_home", "away": "pm_price_away"}))
        out["pm_home"] = out.pm_price_home / (out.pm_price_home + out.pm_price_away)
    return out.reset_index()


def ll(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def blend(sub: pd.DataFrame, market: str) -> None:
    sub = sub.sort_values("start_time")
    y = sub.y.to_numpy()
    lm, lp = logit(sub.p.to_numpy()), logit(sub[market].to_numpy())
    half = len(sub) // 2
    best = None
    for a in np.arange(0.6, 1.41, 0.05):
        for b in np.arange(0.0, 0.81, 0.05):
            v = ll(1 / (1 + np.exp(-(a * lp[:half] + b * lm[:half]))), y[:half])
            if best is None or v < best[0]:
                best = (v, a, b)
    _, a, b = best
    q = 1 / (1 + np.exp(-(a * lp[half:] + b * lm[half:])))
    print(f"  blend (fit on the 1st half): market weight {a:.2f}, model weight {b:.2f}; 2nd half log loss "
          f"market {ll(sub[market].to_numpy()[half:], y[half:]):.4f}, model {ll(sub.p.to_numpy()[half:], y[half:]):.4f}"
          f", blend {ll(q, y[half:]):.4f}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2025-10-01")
    ap.add_argument("--train-from", default="2024-07-01")
    ap.add_argument("--warmup-from", default="2024-01-01")
    args = ap.parse_args()
    since = dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.timezone.utc)
    train_from = dt.datetime.fromisoformat(args.train_from).replace(tzinfo=dt.timezone.utc)
    t0 = time.time()
    with engine().connect() as c:
        m = ft.load_matches(c, dt.datetime.fromisoformat(args.warmup_from).replace(tzinfo=dt.timezone.utc))
    print(f"{len(m)} singles loaded ({time.time() - t0:.0f}s)", flush=True)
    feats = ft.build(m)
    print(f"features built ({time.time() - t0:.0f}s)", flush=True)
    d = m[["id", "start_time", "hs", "aws", "complete", "level"]].rename(columns={"id": "match_id",
                                                                                 "level": "lvl"}) \
        .merge(feats, on="match_id")
    d = d[(d.start_time >= train_from) & d.complete & (d.hs != d.aws)].reset_index(drop=True)
    d["y"] = (d.hs > d.aws).astype(int)
    X = [c for c in feats.columns if c != "match_id"]
    print(f"{len(d)} decided singles from {train_from:%Y-%m-%d}, {len(X)} features", flush=True)

    preds, month = [], since
    now = dt.datetime.now(dt.timezone.utc)
    while month < now:
        nxt = (month + dt.timedelta(days=32)).replace(day=1)
        train, test = d[d.start_time < month], d[(d.start_time >= month) & (d.start_time < nxt)]
        if len(test):
            clf = CatBoostClassifier(loss_function="Logloss", iterations=700, learning_rate=0.04, depth=6,
                                     l2_leaf_reg=6, verbose=False, random_seed=7, thread_count=4, cat_features=CAT)
            clf.fit(train[X], train.y)
            preds.append(test[["match_id", "start_time", "y", "lvl", "elo_p", "blend_p"]]
                         .assign(p=clf.predict_proba(test[X])[:, 1]))
            print(f"  {month:%Y-%m}: train {len(train)}, test {len(test)} ({time.time() - t0:.0f}s)", flush=True)
        month = nxt
    pred = pd.concat(preds, ignore_index=True)
    pred["blend_p"] = pred.blend_p.fillna(pred.elo_p)
    pred = pred.merge(market_probs(pred.match_id.astype(int).tolist()), on="match_id", how="left")
    pred.to_csv(OUT, index=False)
    imp = pd.Series(clf.get_feature_importance(), index=X).sort_values(ascending=False)
    print("\nfeature importance (last model):\n" + imp.head(15).round(1).to_string())

    y = pred.y.to_numpy()
    print(f"\nall {len(pred)} matches: log loss catboost {ll(pred.p.to_numpy(), y):.4f}, elo "
          f"{ll(pred.elo_p.to_numpy(), y):.4f}, surface elo {ll(pred.blend_p.to_numpy(), y):.4f}")
    print("by level (catboost log loss / n):", {lv: (round(ll(g.p.to_numpy(), g.y.to_numpy()), 4), len(g))
                                                for lv, g in pred.groupby("lvl")})
    for label, col in (("bookmaker closing", "bk_home"), ("Polymarket", "pm_home")):
        sub = pred.dropna(subset=[col])
        yy = sub.y.to_numpy()
        print(f"\nvs {label}: {len(sub)} matches")
        for name, c in (("catboost", "p"), ("surface elo", "blend_p"), (label, col)):
            print(f"  {name:<18} log loss {ll(sub[c].to_numpy(), yy):.4f}  brier {brier(sub[c].to_numpy(), yy):.4f}")
        blend(sub, col)

    # favourite calibration (catboost)
    pf = np.where(pred.p >= 0.5, pred.p, 1 - pred.p)
    won = np.where(pred.p >= 0.5, pred.y, 1 - pred.y)
    cal = pd.DataFrame({"p": pf, "won": won})
    cal["bucket"] = pd.cut(cal.p, [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0])
    print("\nfavourite calibration (catboost):")
    print(cal.groupby("bucket", observed=True).agg(n=("won", "size"), model_p=("p", "mean"), won=("won", "mean"))
          .round(3).to_string())

    sub = pred.dropna(subset=["pm_price_home", "pm_price_away"])
    bets = pd.concat([pd.DataFrame({"p": sub.p, "price": sub.pm_price_home + HALF_SPREAD, "won": sub.y}),
                      pd.DataFrame({"p": 1 - sub.p, "price": sub.pm_price_away + HALF_SPREAD, "won": 1 - sub.y})])
    bets["odds"] = 1 / bets.price
    bets["ev"] = bets.p * bets.odds - 1
    print(f"\nvalue singles at the Polymarket price (+{HALF_SPREAD}), {len(sub)} matches:")
    for e in (0.0, 0.03, 0.05, 0.10):
        b = bets[bets.ev >= e]
        print(f"  edge >= {e:.2f}: {len(b):5d} bets, won {b.won.mean():.3f}, mean odds {b.odds.mean():.2f}, "
              f"ROI {(b.won * b.odds - 1).mean():+.3f}")
    for thr in (0.70, 0.80):
        fav = bets[(bets.p >= thr) & (bets.p >= bets.price)]
        print(f"  confident favourites (model p >= {thr:.2f} and >= price): {len(fav)} bets, won {fav.won.mean():.3f},"
              f" model p {fav.p.mean():.3f}, price {fav.price.mean():.3f}, ROI {(fav.won * fav.odds - 1).mean():+.3f}")


if __name__ == "__main__":
    main()
