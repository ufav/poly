"""Football goals model: Dixon-Coles on one pool of leagues and cups, so that teams of different countries meet on
one scale (European cups link the leagues).

  log mu_home = c + hfa + att[home] + weak[away]
  log mu_away = c       + att[away] + weak[home]          (weak = defensive weakness)

Weighted Poisson IRLS on sparse normal equations (thousands of teams), time decay exp(-xi * days ago), a ridge prior
pulling teams with little data to the average; the Dixon-Coles low-score correction rho by grid. A fitted model
gives the score matrix of any pair and from it the probabilities of the markets Polymarket lists: 1x2, totals,
spreads (home handicap line), both teams to score.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import numpy as np
import pandas as pd
import scipy.sparse as sp
from scipy.sparse.linalg import spsolve
from scipy.stats import poisson
from sqlalchemy import text

XI = 0.0019           # time decay per day (half-life ~1 year)
WINDOW = 730          # days of history per fit
RIDGE = 2.0           # prior strength (weighted matches) on team attack / weakness
MAX_GOALS = 10
RHO_GRID = np.round(np.arange(-0.20, 0.101, 0.01), 2)
UEFA_CUPS = ("Лига Чемпионов УЕФА", "Лига Европы УЕФА", "Лига Конференций УЕФА")


def load_pool(conn, until: dt.datetime, days: int = WINDOW) -> pd.DataFrame:
    """Finished men's senior club matches of the pool in [until - days, until): every football-data league, the
    UEFA cups, and every other competitive match of the clubs that played a UEFA cup in the window."""
    return pd.read_sql(text("""
        WITH win AS (
          SELECT m.id, m.league_id, m.start_time, m.home_team_id h, m.away_team_id a, m.home_score hg, m.away_score ag
          FROM matches m JOIN leagues l ON l.id = m.league_id
          WHERE m.sport_id = 1 AND m.status = 'finished' AND m.home_score IS NOT NULL
            AND m.start_time >= :lo AND m.start_time < :hi
            AND l.gender = 'm' AND l.age_group IS NULL AND l.kind <> 'friendly'
            AND l.name !~* '(friendl|товарищ|U\\d\\d|до \\d\\d|резерв|reserve|youth|women)'),
        cups AS (SELECT id FROM leagues WHERE sport_id = 1 AND gender = 'm' AND name = ANY(:cups)),
        fd AS (SELECT entity_id id FROM source_entities WHERE source_id = 1 AND entity_type = 'league'),
        cup_teams AS (SELECT h t FROM win WHERE league_id IN (SELECT id FROM cups)
                      UNION SELECT a FROM win WHERE league_id IN (SELECT id FROM cups))
        SELECT w.* FROM win w JOIN teams th ON th.id = w.h JOIN teams ta ON ta.id = w.a
        WHERE th.kind = 'club' AND ta.kind = 'club' AND th.age_group IS NULL AND ta.age_group IS NULL
          AND (w.league_id IN (SELECT id FROM fd) OR w.league_id IN (SELECT id FROM cups)
               OR w.h IN (SELECT t FROM cup_teams) OR w.a IN (SELECT t FROM cup_teams))"""),
        conn, params={"lo": until - dt.timedelta(days=days), "hi": until, "cups": list(UEFA_CUPS)})


@dataclasses.dataclass
class Model:
    team_index: dict[int, int]
    beta: np.ndarray            # [c, hfa, att(n), weak(n)]
    rho: float
    fitted_at: dt.datetime

    @property
    def n(self) -> int:
        return len(self.team_index)

    def rates(self, home: int, away: int, neutral: bool = False) -> tuple[float, float] | None:
        """Expected goals of a pair; None if a team is unknown to the model."""
        i, j = self.team_index.get(home), self.team_index.get(away)
        if i is None or j is None:
            return None
        c, hfa, n = self.beta[0], 0.0 if neutral else self.beta[1], self.n
        mh = np.exp(c + hfa + self.beta[2 + i] + self.beta[2 + n + j])
        ma = np.exp(c + self.beta[2 + j] + self.beta[2 + n + i])
        return float(mh), float(ma)

    def matrix(self, mh: float, ma: float) -> np.ndarray:
        """P(home goals = x, away goals = y), Dixon-Coles corrected."""
        g = np.arange(MAX_GOALS + 1)
        m = np.outer(poisson.pmf(g, mh), poisson.pmf(g, ma))
        r = self.rho
        m[0, 0] *= 1 - mh * ma * r
        m[0, 1] *= 1 + mh * r
        m[1, 0] *= 1 + ma * r
        m[1, 1] *= 1 - r
        return m / m.sum()


MIN_GAMES = 6         # teams with fewer pool matches are dropped (a stray friendly would rate them wildly)


def prune(df: pd.DataFrame, min_games: int = MIN_GAMES) -> pd.DataFrame:
    for _ in range(3):
        counts = pd.concat([df["h"], df["a"]]).value_counts()
        keep = counts[counts >= min_games].index
        new = df[df["h"].isin(keep) & df["a"].isin(keep)]
        if len(new) == len(df):
            break
        df = new
    return df


def fit(df: pd.DataFrame, at: dt.datetime, xi: float = XI, ridge: float = RIDGE, iters: int = 30) -> Model:
    df = prune(df)
    teams = pd.unique(pd.concat([df["h"], df["a"]]))
    index = {int(t): k for k, t in enumerate(teams)}
    n, m = len(index), len(df)
    hi = df["h"].map(index).to_numpy()
    ai = df["a"].map(index).to_numpy()
    days = (pd.Timestamp(at) - pd.to_datetime(df["start_time"], utc=True)).dt.total_seconds().to_numpy() / 86400
    w = np.exp(-xi * np.clip(days, 0, None))
    p = 2 + 2 * n
    r = np.arange(m)
    # rows 0..m-1 = home goals: c, hfa, att[h], weak[a]; rows m..2m-1 = away goals: c, att[a], weak[h]
    r_all = np.concatenate([np.arange(2 * m), r, r, r, m + r, m + r])
    c_all = np.concatenate([np.zeros(2 * m, int), np.ones(m, int), 2 + hi, 2 + n + ai, 2 + ai, 2 + n + hi])
    X = sp.csr_matrix((np.ones(len(r_all)), (r_all, c_all)), shape=(2 * m, p))
    y = np.concatenate([df["hg"].to_numpy(), df["ag"].to_numpy()]).astype(float)
    ww = np.concatenate([w, w])
    prior = np.zeros(p)
    reg = np.full(p, ridge)
    reg[:2] = 1e-6
    R = sp.diags(reg)
    beta = np.zeros(p)
    beta[0] = np.log(max(y.mean(), 0.1))
    for _ in range(iters):
        eta = X @ beta
        mu = np.exp(eta)
        W = sp.diags(ww * mu)
        z = eta + (y - mu) / mu
        new = spsolve((X.T @ W @ X + R).tocsc(), X.T @ (ww * mu * z) + reg * prior)
        if np.max(np.abs(new - beta)) < 1e-6:
            beta = new
            break
        beta = new
    model = Model(index, beta, 0.0, at)
    # rho on the low scores, weighted the same way
    mh = np.exp(X[:m] @ beta)
    ma = np.exp(X[m:] @ beta)
    hg, ag = df["hg"].to_numpy(), df["ag"].to_numpy()
    best, best_ll = 0.0, -np.inf
    for r in RHO_GRID:
        tau = np.ones(m)
        tau[(hg == 0) & (ag == 0)] = (1 - mh * ma * r)[(hg == 0) & (ag == 0)]
        tau[(hg == 0) & (ag == 1)] = (1 + mh * r)[(hg == 0) & (ag == 1)]
        tau[(hg == 1) & (ag == 0)] = (1 + ma * r)[(hg == 1) & (ag == 0)]
        tau[(hg == 1) & (ag == 1)] = 1 - r
        if np.any(tau <= 0):
            continue
        ll = float(np.sum(w * np.log(tau)))
        if ll > best_ll:
            best, best_ll = float(r), ll
    model.rho = best
    return model


def markets(mat: np.ndarray) -> dict:
    """Market probabilities from a score matrix: 1x2, both teams to score, and functions for any total / spread."""
    g = np.arange(mat.shape[0])
    diff = g[:, None] - g[None, :]
    tot = g[:, None] + g[None, :]
    return {"home": float(mat[diff > 0].sum()), "draw": float(mat[diff == 0].sum()),
            "away": float(mat[diff < 0].sum()), "btts_yes": float(mat[1:, 1:].sum()),
            "_diff": diff, "_tot": tot, "_mat": mat}


def p_total_over(mk: dict, line: float) -> float | None:
    """P(over line) without pushes (integer lines: pushes removed); quarter lines -> None."""
    if (line * 4) % 2:  # .25 / .75
        return None
    over = mk["_mat"][mk["_tot"] > line].sum()
    push = mk["_mat"][mk["_tot"] == line].sum()
    return float(over / (1 - push)) if push < 1 else None


def p_spread_home(mk: dict, line: float) -> float | None:
    """P(home covers the home handicap line), pushes removed; quarter lines -> None."""
    if (line * 4) % 2:
        return None
    m = mk["_mat"]
    cover = m[mk["_diff"] + line > 0].sum()
    push = m[mk["_diff"] + line == 0].sum()
    return float(cover / (1 - push)) if push < 1 else None
