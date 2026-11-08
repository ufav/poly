"""Walk-forward check of the NBA model (ml_engine/basketball/features.py): A = CatBoost on all the features, B =
logistic on Elo / net rating / absences / rest, refit every month from --since on the games before (the first
months of 2024/25 only warm the ratings up); vs the Polymarket moneyline before tip-off: log loss, the market blend
weight, favourite calibration, confident favourites and value singles at the price + 1 c.

  python -m backtesting.nba_cb [--since 2025-10-01] [--league NBA]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from backtesting.tennis_cb import blend, brier, ll, market_probs
from core.db import engine
from ml_engine.basketball import features as ft

OUT = Path(__file__).resolve().parents[1] / "data" / "nba_cb_predictions.csv"
B_COLS = ["elo_diff", "d_net", "d_miss_share", "d_miss_top", "h_b2b", "a_b2b", "d_rest", "d_last10"]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2025-10-01")
    ap.add_argument("--train-from", default="2025-01-01")
    ap.add_argument("--league", default="NBA")
    args = ap.parse_args()
    with engine().connect() as c:
        m = ft.load(c, dt.datetime(2024, 10, 1, tzinfo=dt.timezone.utc), args.league)
    feats = ft.build(m)
    d = m[["id", "start_time", "hs", "aws"]].rename(columns={"id": "match_id"}).merge(feats, on="match_id")
    d = d[(d.start_time >= pd.Timestamp(args.train_from, tz="UTC")) & (d.hs != d.aws)].reset_index(drop=True)
    d["y"] = (d.hs > d.aws).astype(int)
    X = [c for c in feats.columns if c != "match_id"]
    print(f"{len(m)} games loaded, {len(d)} with features from {args.train_from}, {len(X)} features", flush=True)
    preds, month = [], pd.Timestamp(args.since, tz="UTC")
    while month < pd.Timestamp.now(tz="UTC"):
        nxt = (month + pd.Timedelta(days=32)).replace(day=1)
        tr, te = d.start_time < month, (d.start_time >= month) & (d.start_time < nxt)
        if te.any() and tr.sum() > 200:
            a = CatBoostClassifier(loss_function="Logloss", iterations=400, learning_rate=0.03, depth=4,
                                   l2_leaf_reg=10, verbose=False, random_seed=7, thread_count=4)
            a.fit(d.loc[tr, X], d.y[tr])
            b = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=500))
            b.fit(d.loc[tr, B_COLS].fillna(0), d.y[tr])
            preds.append(d.loc[te, ["match_id", "start_time", "y", "elo_p"]].assign(
                p=a.predict_proba(d.loc[te, X])[:, 1], p_b=b.predict_proba(d.loc[te, B_COLS].fillna(0))[:, 1]))
            print(f"  {month:%Y-%m}: train {tr.sum()}, test {te.sum()}", flush=True)
        month = nxt
    pred = pd.concat(preds, ignore_index=True)
    pred = pred.merge(market_probs(pred.match_id.astype(int).tolist()), on="match_id", how="left")
    pred.to_csv(OUT, index=False)
    imp = pd.Series(a.get_feature_importance(), index=X).sort_values(ascending=False)
    print("\nfeature importance (last A):\n" + imp.head(12).round(1).to_string())
    print("B weights:", dict(zip(B_COLS, np.round(b[-1].coef_[0], 3))))
    y = pred.y.to_numpy()
    print(f"\nall {len(pred)}: log loss A {ll(pred.p.to_numpy(), y):.4f}, B {ll(pred.p_b.to_numpy(), y):.4f}, "
          f"Elo {ll(pred.elo_p.to_numpy(), y):.4f}")
    sub = pred.dropna(subset=["pm_home"])
    yy = sub.y.to_numpy()
    print(f"\nvs Polymarket: {len(sub)} games")
    for name, c in (("A", "p"), ("B", "p_b"), ("Elo", "elo_p"), ("Polymarket", "pm_home")):
        print(f"  {name:<11} log loss {ll(sub[c].to_numpy(), yy):.4f}  brier {brier(sub[c].to_numpy(), yy):.4f}")
    blend(sub, "pm_home")
    bets = pd.concat([pd.DataFrame({"a": sub.p, "b": sub.p_b, "price": sub.pm_price_home + 0.01, "won": sub.y}),
                      pd.DataFrame({"a": 1 - sub.p, "b": 1 - sub.p_b, "price": sub.pm_price_away + 0.01,
                                    "won": 1 - sub.y})])
    bets["odds"] = 1 / bets.price

    def rep(g):
        if not len(g):
            return "0 bets"
        r = g.won * g.odds - 1
        return f"{len(g):4d} bets, won {g.won.mean():.3f}, price {g.price.mean():.3f}, ROI {r.mean():+.3f} " \
               f"(± {np.sqrt(r.var() / len(g)):.3f})"
    print("\nconfident favourites (p >= thr and >= price):")
    for thr in (0.65, 0.70, 0.75, 0.80):
        print(f"  {thr:.2f}  A: {rep(bets[(bets.a >= thr) & (bets.a >= bets.price)])}")
        print(f"        B: {rep(bets[(bets.b >= thr) & (bets.b >= bets.price)])}")
    print("value singles A (p * odds - 1 >= edge):")
    for e in (0.0, 0.05, 0.10):
        print(f"  {e:.2f}: {rep(bets[bets.a / bets.price - 1 >= e])}")


if __name__ == "__main__":
    main()
