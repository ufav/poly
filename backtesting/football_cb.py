"""Walk-forward check of the football feature model (docs/md1.md 8.2): CatBoost on the pre-match features
(ml_engine/football/features.py) + the Dixon-Coles rates of the weekly refit, refit every month from --since on
everything before that month (training from --train-from), compared with the bookmakers' closing and Polymarket like
backtesting/football_dc.py: log loss / Brier, the market blend weight (does the model add information?), favourite
calibration, value singles at the Polymarket price by edge, and how often the confident favourites win.

  python -m backtesting.football_cb [--since 2025-08-01] [--train-from 2023-08-01]
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

from backtesting import football_dc as fdc
from core.db import engine
from ml_engine.football import features as ft

DATA = Path(__file__).resolve().parents[1] / "data"
DC_FULL = DATA / "football_dc_predictions_full.csv"
OUT = DATA / "football_cb_predictions.csv"
HALF_SPREAD = fdc.HALF_SPREAD
DC_COLS = ["mu_home", "mu_away", "p_home", "p_draw", "p_away"]


def dc_predictions(train_from: dt.datetime) -> pd.DataFrame:
    """Weekly Dixon-Coles predictions from train_from (cached in DC_FULL; the stored 2025-08+ run is reused)."""
    if DC_FULL.exists():
        df = pd.read_csv(DC_FULL)
    else:
        parts = [pd.read_csv(fdc.OUT)] if fdc.OUT.exists() else []
        first = pd.to_datetime(parts[0].start_time, utc=True, format="ISO8601").min().to_pydatetime() if parts else             dt.datetime.now(dt.timezone.utc)
        if train_from < first:
            parts.append(fdc.walk_forward(train_from, first, fdc.dc.XI))
        df = pd.concat(parts, ignore_index=True).drop_duplicates("match_id")
        df.to_csv(DC_FULL, index=False)
    return df[["match_id", *DC_COLS]].rename(columns={c: f"dc_{c}" for c in DC_COLS})


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2025-08-01")
    ap.add_argument("--train-from", default="2023-08-01")
    ap.add_argument("--warmup-from", default="2021-07-01")
    args = ap.parse_args()
    since = dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.timezone.utc)
    train_from = dt.datetime.fromisoformat(args.train_from).replace(tzinfo=dt.timezone.utc)
    t0 = time.time()
    with engine().connect() as c:
        m = ft.load_matches(c, dt.datetime.fromisoformat(args.warmup_from).replace(tzinfo=dt.timezone.utc))
    print(f"{len(m)} pool matches loaded ({time.time() - t0:.0f}s)", flush=True)
    feats = ft.build(m)
    print(f"features built ({time.time() - t0:.0f}s)", flush=True)
    dcp = dc_predictions(train_from)
    print(f"Dixon-Coles predictions: {len(dcp)} ({time.time() - t0:.0f}s)", flush=True)
    d = m[["id", "start_time", "league_id", "hg", "ag"]].rename(columns={"id": "match_id"}) \
        .merge(feats, on="match_id").merge(dcp, on="match_id", how="inner")
    d = d[d.start_time >= train_from].reset_index(drop=True)
    d["y"] = np.select([d.hg > d.ag, d.hg == d.ag], [0, 1], 2)
    X_cols = [c for c in d.columns if c not in ("match_id", "start_time", "league_id", "hg", "ag", "y")]
    print(f"{len(d)} matches with features from {train_from:%Y-%m-%d}, {len(X_cols)} features", flush=True)

    preds, month = [], since
    now = dt.datetime.now(dt.timezone.utc)
    while month < now:
        nxt = (month + dt.timedelta(days=32)).replace(day=1)
        train = d[d.start_time < month]
        test = d[(d.start_time >= month) & (d.start_time < nxt)]
        if len(test):
            clf = CatBoostClassifier(loss_function="MultiClass", iterations=600, learning_rate=0.04, depth=5,
                                     l2_leaf_reg=6, verbose=False, random_seed=7, thread_count=4)
            clf.fit(train[X_cols], train.y)
            p = clf.predict_proba(test[X_cols])
            preds.append(test[["match_id", "start_time", "league_id", "hg", "ag", "dc_p_home", "dc_p_draw",
                               "dc_p_away"]].assign(p_home=p[:, 0], p_draw=p[:, 1], p_away=p[:, 2]))
            print(f"  {month:%Y-%m}: train {len(train)}, test {len(test)} ({time.time() - t0:.0f}s)", flush=True)
        month = nxt
    pred = pd.concat(preds, ignore_index=True)
    pred = pred.merge(fdc.market_probs(pred.match_id.astype(int).tolist()), on="match_id", how="left")
    pred.to_csv(OUT, index=False)
    imp = pd.Series(clf.get_feature_importance(), index=X_cols).sort_values(ascending=False)
    print("\nfeature importance (last model):\n" + imp.head(20).round(1).to_string())

    model, dcc = ["p_home", "p_draw", "p_away"], ["dc_p_home", "dc_p_draw", "dc_p_away"]
    bk, pm = ["bk_home", "bk_draw", "bk_away"], ["pm_home", "pm_draw", "pm_away"]
    for label, sub, cols in (("vs bookmaker closing", pred.dropna(subset=bk), {"catboost": model, "dixon_coles": dcc,
                                                                                "bookmaker": bk}),
                             ("vs Polymarket", pred.dropna(subset=pm), {"catboost": model, "dixon_coles": dcc,
                                                                         "polymarket": pm})):
        print(f"\n{label}: {len(sub)} matches")
        print(fdc.scores(sub, cols).round(4).to_string())
        blend(sub, model, bk if "bookmaker" in cols else pm)
    calibration(pred, model)
    value_singles(pred, model)


def blend(sub: pd.DataFrame, model: list[str], market: list[str]) -> None:
    sub = sub.sort_values("start_time")
    y = np.select([sub.hg > sub.ag, sub.hg == sub.ag], [0, 1], 2)
    M, P = sub[model].to_numpy(), sub[market].to_numpy()
    half = len(sub) // 2

    def ll(q, yy):
        return float(-np.mean(np.log(np.clip(q[np.arange(len(yy)), yy], 1e-9, 1))))
    best = None
    for a in np.arange(0.6, 1.41, 0.05):
        for b in np.arange(0.0, 0.81, 0.05):
            q = P[:half] ** a * M[:half] ** b
            q /= q.sum(1, keepdims=True)
            v = ll(q, y[:half])
            if best is None or v < best[0]:
                best = (v, a, b)
    _, a, b = best
    q = P[half:] ** a * M[half:] ** b
    q /= q.sum(1, keepdims=True)
    print(f"  blend (fit on the 1st half): market weight {a:.2f}, model weight {b:.2f}; 2nd half log loss "
          f"market {ll(P[half:], y[half:]):.4f}, model {ll(M[half:], y[half:]):.4f}, blend {ll(q, y[half:]):.4f}")


def calibration(pred: pd.DataFrame, model: list[str]) -> None:
    y = np.select([pred.hg > pred.ag, pred.hg == pred.ag], [0, 1], 2)
    p = pred[model].to_numpy()
    cal = pd.DataFrame({"p": p.max(1), "won": (p.argmax(1) == y).astype(int)})
    cal["bucket"] = pd.cut(cal.p, [0.3, 0.4, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 1.0])
    print("\nfavourite calibration (catboost):")
    print(cal.groupby("bucket", observed=True).agg(n=("won", "size"), model_p=("p", "mean"), won=("won", "mean"))
          .round(3).to_string())


def value_singles(pred: pd.DataFrame, model: list[str]) -> None:
    sub = pred.dropna(subset=["pm_price_home", "pm_price_draw", "pm_price_away"])
    y = np.select([sub.hg > sub.ag, sub.hg == sub.ag], [0, 1], 2)
    bets = pd.concat([pd.DataFrame({"p": sub[model[k]], "price": sub[f"pm_price_{o}"] + HALF_SPREAD,
                                    "won": (y == k).astype(int)}) for k, o in enumerate(("home", "draw", "away"))])
    bets["odds"] = 1 / bets.price
    bets["ev"] = bets.p * bets.odds - 1
    print(f"\nvalue singles at the Polymarket price (+{HALF_SPREAD}), {len(sub)} matches:")
    for e in (0.0, 0.03, 0.05, 0.10):
        b = bets[bets.ev >= e]
        print(f"  edge >= {e:.2f}: {len(b):5d} bets, won {b.won.mean():.3f}, mean odds {b.odds.mean():.2f}, "
              f"ROI {(b.won * b.odds - 1).mean():+.3f}")
    fav = bets[(bets.p >= 0.70) & (bets.p >= bets.price)]
    print(f"  confident favourites (model p >= 0.70 and >= price): {len(fav)} bets, won {fav.won.mean():.3f}, "
          f"model p {fav.p.mean():.3f}, price {fav.price.mean():.3f}, ROI {(fav.won * fav.odds - 1).mean():+.3f}")


if __name__ == "__main__":
    main()
