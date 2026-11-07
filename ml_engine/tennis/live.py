"""Tennis models for the upcoming matches: A = CatBoost on the features (backtesting/tennis_cb.py), B = logistic on
the form / circumstances (backtesting/tennis_abc.py), C = mean of the logits. Trained on all decided singles since
TRAIN_FROM, cached in data/models and refit when older than a day; the features of the upcoming matches are built
on the same history (ml_engine/tennis/features.py) - the same way as in the backtest.
"""

from __future__ import annotations

import datetime as dt
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from core.db import engine
from ml_engine.tennis import features as ft

MODELS = Path(__file__).resolve().parents[2] / "data" / "models"
WARMUP_FROM = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
TRAIN_FROM = pd.Timestamp("2024-07-01", tz="UTC")
CAT = ["surface", "level"]
B_COLS = ["d_form7", "d_over7", "d_over7_s", "log_rank_ratio", "d_m7", "d_g7", "d_rest", "h2h_share"]
VERSION = "tennis_v1"
MAX_AGE_H = 24


def b_matrix(df: pd.DataFrame) -> pd.DataFrame:
    X = df[B_COLS].copy()
    X["h2h_share"] = X.h2h_share - 0.5
    miss = pd.DataFrame({f"miss_{c}": X[c].isna().astype(int) for c in B_COLS}, index=df.index)
    return pd.concat([X.fillna(0.0), miss], axis=1)


def _features(upcoming: list[int]) -> tuple[pd.DataFrame, pd.DataFrame]:
    with engine().connect() as c:
        m = ft.load_matches(c, WARMUP_FROM, upcoming=upcoming)
    return m, ft.build(m)


def train() -> dict:
    t0 = time.time()
    m, feats = _features([])
    d = m[["id", "start_time", "hs", "aws", "complete"]].rename(columns={"id": "match_id"}).merge(feats, on="match_id")
    d = d[(d.start_time >= TRAIN_FROM) & d.complete & (d.hs != d.aws)]
    y = (d.hs > d.aws).astype(int)
    X = [c for c in feats.columns if c != "match_id"]
    a = CatBoostClassifier(loss_function="Logloss", iterations=700, learning_rate=0.04, depth=6, l2_leaf_reg=6,
                           verbose=False, random_seed=7, thread_count=4, cat_features=CAT)
    a.fit(d[X], y)
    b = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=500)).fit(b_matrix(d), y)
    bundle = {"a": a, "b": b, "X": X, "trained_at": dt.datetime.now(dt.timezone.utc), "n": len(d),
              "version": VERSION}
    MODELS.mkdir(parents=True, exist_ok=True)
    with open(MODELS / f"{VERSION}.pkl", "wb") as f:
        pickle.dump(bundle, f)
    return {"n": len(d), "seconds": round(time.time() - t0)}


def load(refit: bool = True) -> dict:
    path = MODELS / f"{VERSION}.pkl"
    if refit and (not path.exists() or
                  dt.datetime.now(dt.timezone.utc) - pickle.load(open(path, "rb"))["trained_at"]
                  > dt.timedelta(hours=MAX_AGE_H)):
        train()
    with open(path, "rb") as f:
        return pickle.load(f)


def predict(match_ids: list[int], bundle: dict | None = None) -> pd.DataFrame:
    """p of the home side by A, B, C for the given upcoming match ids (missing if a player is unknown to the pool)."""
    bundle = bundle or load()
    if not match_ids:
        return pd.DataFrame(columns=["match_id", "p_a", "p_b", "p_c"])
    _, feats = _features(match_ids)
    f = feats[feats.match_id.isin(match_ids)].reset_index(drop=True)
    if f.empty:
        return pd.DataFrame(columns=["match_id", "p_a", "p_b", "p_c"])
    pa = bundle["a"].predict_proba(f[bundle["X"]])[:, 1]
    pb = bundle["b"].predict_proba(b_matrix(f))[:, 1]

    def logit(p):
        p = np.clip(p, 1e-6, 1 - 1e-6)
        return np.log(p / (1 - p))
    pc = 1 / (1 + np.exp(-(logit(pa) + logit(pb)) / 2))
    return pd.DataFrame({"match_id": f.match_id, "p_a": pa, "p_b": pb, "p_c": pc,
                         "n_h": f.n_h, "n_a": f.n_a, "elo_p": f.elo_p})
