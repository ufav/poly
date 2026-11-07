"""Tennis: three approaches on the same matches vs the Polymarket price (the experiment of 2026-11-08):
  A - the feature model (CatBoost, backtesting/tennis_cb.py predictions);
  B - current form and circumstances only, no boosting: logistic regression on the last-7 form adjusted for the
      opponents (result - Elo expectation, overall and on the surface), raw last-7 wins, ranking, load (matches /
      games in 7 days, rest), head-to-head - refit every month on the matches before;
  C - A and B combined (mean of the logits); 'consensus' = A and B both confident.
Per approach: log loss, and the confident favourites (p >= threshold and >= the price) bought at the price +1 c.

  python -m backtesting.tennis_abc
"""

from __future__ import annotations

import datetime as dt
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from backtesting.tennis_cb import OUT as A_PRED, ll, logit
from core.db import engine
from ml_engine.tennis import features as ft

B_COLS = ["d_form7", "d_over7", "d_over7_s", "log_rank_ratio", "d_m7", "d_g7", "d_rest", "h2h_share"]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with engine().connect() as c:
        m = ft.load_matches(c, dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc))
    feats = ft.build(m)
    d = m[["id", "start_time", "hs", "aws", "complete"]].rename(columns={"id": "match_id"}).merge(feats, on="match_id")
    d = d[(d.start_time >= pd.Timestamp("2024-07-01", tz="UTC")) & d.complete & (d.hs != d.aws)].reset_index(drop=True)
    d["y"] = (d.hs > d.aws).astype(int)
    X = d[B_COLS].copy()
    X["h2h_share"] = X.h2h_share - 0.5
    for c in B_COLS:  # missing -> 0 + a flag (no ranking for ITF / UTR players ...)
        d[f"miss_{c}"] = X[c].isna().astype(int)
        X[c] = X[c].fillna(0.0)
    X = pd.concat([X, d[[f"miss_{c}" for c in B_COLS]]], axis=1)
    a = pd.read_csv(A_PRED)[["match_id", "p", "pm_home", "pm_price_home", "pm_price_away"]]
    a = a.rename(columns={"p": "p_a"})
    preds = []
    month = pd.Timestamp("2025-10-01", tz="UTC")
    while month < pd.Timestamp.now(tz="UTC"):
        nxt = (month + pd.Timedelta(days=32)).replace(day=1)
        tr, te = d.start_time < month, (d.start_time >= month) & (d.start_time < nxt)
        if te.any():
            clf = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=500))
            clf.fit(X[tr], d.y[tr])
            preds.append(d.loc[te, ["match_id", "start_time", "y"]].assign(p_b=clf.predict_proba(X[te])[:, 1]))
        month = nxt
    coef = pd.Series(clf[-1].coef_[0], index=X.columns).round(3)
    print("approach B (logistic) weights of the last model:\n" + coef[coef.abs() > 0.01].to_string())
    p = pd.concat(preds).merge(a, on="match_id", how="inner")
    p["p_c"] = 1 / (1 + np.exp(-(logit(p.p_a) + logit(p.p_b)) / 2))
    y = p.y.to_numpy()
    print(f"\n{len(p)} matches with A and B; log loss A {ll(p.p_a.to_numpy(), y):.4f}, B {ll(p.p_b.to_numpy(), y):.4f}, "
          f"C {ll(p.p_c.to_numpy(), y):.4f}")
    pm = p.dropna(subset=["pm_home"])
    yy = pm.y.to_numpy()
    print(f"{len(pm)} with a Polymarket price; log loss A {ll(pm.p_a.to_numpy(), yy):.4f}, "
          f"B {ll(pm.p_b.to_numpy(), yy):.4f}, C {ll(pm.p_c.to_numpy(), yy):.4f}, "
          f"Polymarket {ll(pm.pm_home.to_numpy(), yy):.4f}")
    rows = []
    for side, price, won in (("home", pm.pm_price_home, pm.y), ("away", pm.pm_price_away, 1 - pm.y)):
        f = (lambda q: q) if side == "home" else (lambda q: 1 - q)
        rows.append(pd.DataFrame({"a": f(pm.p_a), "b": f(pm.p_b), "c": f(pm.p_c), "price": price + 0.01,
                                  "won": won, "t": pm.start_time}))
    b = pd.concat(rows)
    b["odds"] = 1 / b.price

    def rep(g: pd.DataFrame) -> str:
        if not len(g):
            return "0 bets"
        roi = (g.won * g.odds - 1).mean()
        se = np.sqrt((g.won * g.odds - 1).var() / len(g))
        return (f"{len(g):5d} bets, won {g.won.mean():.3f}, price {g.price.mean():.3f}, ROI {roi:+.3f} "
                f"(± {se:.3f})")
    print("\nconfident favourites, bought at the Polymarket price + 1 c:")
    for thr in (0.75, 0.80, 0.85):
        print(f"  p >= {thr:.2f}:")
        for name in ("a", "b", "c"):
            g = b[(b[name] >= thr) & (b[name] >= b.price)]
            print(f"    {name.upper()}: {rep(g)}")
        g = b[(b.a >= thr) & (b.b >= thr) & (b.a >= b.price) & (b.b >= b.price)]
        print(f"    consensus A and B: {rep(g)}")


if __name__ == "__main__":
    main()
