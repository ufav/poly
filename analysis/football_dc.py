"""Football: Dixon-Coles goals model per league, walk-forward (refit every week on the matches before it), vs the
closing and the opening market (data/football.sqlite, fd_matches from scripts/collect_football.py).

Model: log mu_home = c + hfa + att_home + weak_away, log mu_away = c + att_away + weak_home (weak = defensive
weakness), Poisson IRLS with time decay exp(-xi * days ago) over the last WINDOW days of the league and a ridge prior
pulling teams with little data to the league average; Dixon-Coles low-score correction rho by grid. From the score
matrix: 1X2 and over/under 2.5. Strength can also learn from shots on target (--shots a: fit on (1-a) * goals +
a * SoT * league conversion), and teams new to a top division get a weaker prior (--promoted d: attack -d,
weakness +d).

Benchmarks: no-vig closing probabilities - Pinnacle (PSC*, PC>2.5) when present, else market average closing - and
opening (main leagues only: Pinnacle PS*, else market average Avg*, taken ~1-2 days before the match). Metrics: log
loss model vs market; flat bets when model p * odds - 1 >= edge at the closing and at the opening odds; CLV of the
opening bets = opening odds / closing odds - 1 (same source).

  python analysis/football_dc.py [--since 2012] [--divs E0,D1,...] [--xi 0.0019] [--shots 0.5] [--promoted 0.15]
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pmbot import footballdata as fd  # noqa: E402

WINDOW = 730          # days of history per fit
RIDGE = 2.0           # prior strength (in weighted matches) on team attack / weakness
TOP_DIVS = {"E0", "SC0", "D1", "I1", "SP1", "F1", "N1", "B1", "P1", "T1", "G1", *fd.EXTRA}
MAX_GOALS = 10
RHO_GRID = np.round(np.arange(-0.20, 0.101, 0.01), 2)
EDGES = (0.0, 0.03, 0.05, 0.10)


def fit(home: np.ndarray, away: np.ndarray, hg: np.ndarray, ag: np.ndarray, w: np.ndarray, n: int,
        yh: np.ndarray | None = None, ya: np.ndarray | None = None, prior: np.ndarray | None = None,
        iters: int = 25) -> tuple[np.ndarray, float]:
    """Weighted (quasi-)Poisson IRLS on yh / ya (default the goals) with a ridge prior around `prior`;
    returns beta = [c, hfa, att(n), weak(n)] and the Dixon-Coles rho fitted on the goals."""
    m = len(home)
    p = 2 + 2 * n
    X = np.zeros((2 * m, p))
    X[:, 0] = 1
    X[:m, 1] = 1
    r = np.arange(m)
    X[r, 2 + home] = 1
    X[r, 2 + n + away] = 1
    X[m + r, 2 + away] = 1
    X[m + r, 2 + n + home] = 1
    y = np.concatenate([hg if yh is None else yh, ag if ya is None else ya]).astype(float)
    ww = np.concatenate([w, w])
    R = np.zeros(p)
    R[2:] = RIDGE
    m0 = np.zeros(p) if prior is None else prior
    beta = m0.copy()
    beta[0] = math.log(max(y.mean(), 0.1))
    for _ in range(iters):
        eta = X @ beta
        mu = np.exp(eta)
        z = eta + (y - mu) / mu
        W = ww * mu
        A = X.T @ (X * W[:, None]) + np.diag(R)
        new = np.linalg.solve(A, X.T @ (W * z) + R * m0)
        if np.max(np.abs(new - beta)) < 1e-6:
            beta = new
            break
        beta = new
    lam = np.exp(X[:m] @ beta)
    mu_a = np.exp(X[m:] @ beta)
    best, best_ll = 0.0, -1e18
    s00, s01 = (hg == 0) & (ag == 0), (hg == 0) & (ag == 1)
    s10, s11 = (hg == 1) & (ag == 0), (hg == 1) & (ag == 1)
    for rho in RHO_GRID:
        tau = np.ones(m)
        tau[s00] = 1 - lam[s00] * mu_a[s00] * rho
        tau[s01] = 1 + lam[s01] * rho
        tau[s10] = 1 + mu_a[s10] * rho
        tau[s11] = 1 - rho
        if np.any(tau <= 0):
            continue
        ll = float(np.sum(w * np.log(tau)))
        if ll > best_ll:
            best, best_ll = float(rho), ll
    return beta, best


def score_matrix(lam: float, mu: float, rho: float) -> np.ndarray:
    k = np.arange(MAX_GOALS + 1)
    fact = np.array([math.factorial(int(i)) for i in k], dtype=float)
    ph = np.exp(-lam) * lam ** k / fact
    pa = np.exp(-mu) * mu ** k / fact
    M = np.outer(ph, pa)
    M[0, 0] *= 1 - lam * mu * rho
    M[0, 1] *= 1 + lam * rho
    M[1, 0] *= 1 + mu * rho
    M[1, 1] *= 1 - rho
    return M / M.sum()


def no_vig(*odds) -> list[float] | None:
    if any(o is None or not o > 1 or (isinstance(o, float) and math.isnan(o)) for o in odds):
        return None
    inv = [1 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv]


def load(conn: sqlite3.Connection, since: int, divs: list[str] | None) -> pd.DataFrame:
    q = """SELECT div, season, date, home, away, fthg, ftag, hst, ast, psh, psd, psa, psch, pscd, psca,
                  avgch, avgcd, avgca, pc_over25, pc_under25, avgc_over25, avgc_under25, odds
           FROM fd_matches WHERE season >= ?"""
    df = pd.read_sql_query(q, conn, params=(since - 3,))
    if divs:
        df = df[df["div"].isin(divs)].copy()
    odds = df.pop("odds").map(json.loads)
    for col, key in (("avgh", "AvgH"), ("avgd", "AvgD"), ("avga", "AvgA")):
        df[col] = odds.map(lambda o, k=key: o.get(k))
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["div", "date"]).reset_index(drop=True)


def walk_forward(g: pd.DataFrame, since: int, xi: float, shots: float, promoted: float) -> pd.DataFrame:
    """Predictions of one league: refit every week on the WINDOW days before it."""
    g = g.reset_index(drop=True)
    div = g["div"].iloc[0]
    teams = {t: i for i, t in enumerate(pd.unique(pd.concat([g["home"], g["away"]])))}
    hi, ai = g["home"].map(teams).to_numpy(), g["away"].map(teams).to_numpy()
    hg, ag = g["fthg"].to_numpy(), g["ftag"].to_numpy()
    hst, ast = g["hst"].to_numpy(dtype=float), g["ast"].to_numpy(dtype=float)
    has_sot = ~np.isnan(hst) & ~np.isnan(ast)
    seasons = g["season"].to_numpy()
    in_season = defaultdict(set)  # season -> team ids that played in this div
    for s_, h_, a_ in zip(seasons, hi, ai):
        in_season[s_].update((h_, a_))
    days = (g["date"] - g["date"].min()).dt.days.to_numpy()
    eval_rows = g.index[g["season"] >= since]
    out = []
    if not len(eval_rows):
        return pd.DataFrame(out)
    start = g.loc[eval_rows[0], "date"]
    week_starts = pd.date_range(start.normalize() - pd.Timedelta(days=start.weekday()), g["date"].max(), freq="7D")
    tot = np.add.outer(np.arange(MAX_GOALS + 1), np.arange(MAX_GOALS + 1))
    for ws in week_starts:
        we = ws + pd.Timedelta(days=7)
        target = g.index[(g["date"] >= ws) & (g["date"] < we) & (g["season"] >= since)].to_numpy()
        if not len(target):
            continue
        d0 = (ws - g["date"].min()).days
        hist = (days < d0) & (days >= d0 - WINDOW)
        if hist.sum() < 150:
            continue
        idx = np.where(hist)[0]
        used = np.unique(np.concatenate([hi[idx], ai[idx], hi[target], ai[target]]))
        local = {t: k for k, t in enumerate(used)}
        n = len(used)
        lh = np.array([local[t] for t in hi[idx]])
        la = np.array([local[t] for t in ai[idx]])
        w = np.exp(-xi * (d0 - days[idx]))
        yh = ya = None
        if shots > 0 and has_sot[idx].any():
            sot = idx[has_sot[idx]]
            conv = (hg[sot].sum() + ag[sot].sum()) / max(hst[sot].sum() + ast[sot].sum(), 1)
            mix = has_sot[idx]
            yh = np.where(mix, (1 - shots) * hg[idx] + shots * conv * np.nan_to_num(hst[idx]), hg[idx])
            ya = np.where(mix, (1 - shots) * ag[idx] + shots * conv * np.nan_to_num(ast[idx]), ag[idx])
        prior = None
        prev = in_season.get(seasons[target[0]] - 1)
        if promoted > 0 and div in TOP_DIVS and prev:
            prior = np.zeros(2 + 2 * n)
            for t, k in local.items():
                if t not in prev:  # new to the division this season
                    prior[2 + k], prior[2 + n + k] = -promoted, promoted
        beta, rho = fit(lh, la, hg[idx], ag[idx], w, n, yh, ya, prior)
        for r in target:
            th, ta = local[hi[r]], local[ai[r]]
            lam = math.exp(beta[0] + beta[1] + beta[2 + th] + beta[2 + n + ta])
            mu = math.exp(beta[0] + beta[2 + ta] + beta[2 + n + th])
            M = score_matrix(lam, mu, rho)
            out.append({"i": r, "pH": float(np.tril(M, -1).sum()), "pD": float(np.trace(M)),
                        "pA": float(np.triu(M, 1).sum()), "pO": float(M[tot >= 3].sum())})
    p = pd.DataFrame(out)
    print(f"  {div}: {len(p)} predictions", flush=True)
    return p.join(g, on="i") if len(p) else p


def evaluate(pred: pd.DataFrame) -> None:
    def blank():
        return {"n": 0, "ll_m": 0.0, "ll_k": 0.0, "n_ou": 0, "ll_m_ou": 0.0, "ll_k_ou": 0.0,
                "n_op": 0, "ll_m_op": 0.0, "ll_k_op": 0.0,
                **{f"b{e}": [0, 0.0] for e in EDGES}, **{f"o{e}": [0, 0.0] for e in EDGES},
                **{f"op{e}": [0, 0.0, 0.0] for e in EDGES}}
    rows = defaultdict(blank)
    for m in pred.itertuples():
        res = "H" if m.fthg > m.ftag else "D" if m.fthg == m.ftag else "A"
        model = {"H": m.pH, "D": m.pD, "A": m.pA}
        src = (m.psch, m.pscd, m.psca) if no_vig(m.psch, m.pscd, m.psca) else (m.avgch, m.avgcd, m.avgca)
        mk = no_vig(*src)
        pin_open = no_vig(m.psh, m.psd, m.psa) and no_vig(m.psch, m.pscd, m.psca)
        op_src = (m.psh, m.psd, m.psa) if pin_open else (m.avgh, m.avgd, m.avga)
        cl_src = (m.psch, m.pscd, m.psca) if pin_open else (m.avgch, m.avgcd, m.avgca)
        op = no_vig(*op_src) if no_vig(*cl_src) else None
        ou_src = (m.pc_over25, m.pc_under25) if no_vig(m.pc_over25, m.pc_under25) \
            else (m.avgc_over25, m.avgc_under25)
        ou = no_vig(*ou_src)
        for key in (m.div, "ALL"):
            r = rows[key]
            if mk:
                r["n"] += 1
                r["ll_m"] -= math.log(max(model[res], 1e-12))
                r["ll_k"] -= math.log(max(dict(zip("HDA", mk))[res], 1e-12))
                for e in EDGES:
                    for side, odds in zip("HDA", src):
                        if model[side] * odds - 1 >= e:
                            r[f"b{e}"][0] += 1
                            r[f"b{e}"][1] += (odds - 1) if side == res else -1
            if op:
                r["n_op"] += 1
                r["ll_m_op"] -= math.log(max(model[res], 1e-12))
                r["ll_k_op"] -= math.log(max(dict(zip("HDA", op))[res], 1e-12))
                for e in EDGES:
                    for side, odds, close in zip("HDA", op_src, cl_src):
                        if model[side] * odds - 1 >= e:
                            b = r[f"op{e}"]
                            b[0] += 1
                            b[1] += (odds - 1) if side == res else -1
                            b[2] += odds / close - 1
            if ou:
                over = m.fthg + m.ftag >= 3
                r["n_ou"] += 1
                r["ll_m_ou"] -= math.log(max(m.pO if over else 1 - m.pO, 1e-12))
                r["ll_k_ou"] -= math.log(max(ou[0] if over else ou[1], 1e-12))
                for e in EDGES:
                    for prob, odds, won in ((m.pO, ou_src[0], over), (1 - m.pO, ou_src[1], not over)):
                        if prob * odds - 1 >= e:
                            r[f"o{e}"][0] += 1
                            r[f"o{e}"][1] += (odds - 1) if won else -1

    keys = sorted(rows, key=lambda k: (k == "ALL", k))

    def roi(x):
        return f"{x[1] / x[0]:+6.1%} ({x[0]})" if x[0] else "—"
    head = " ".join(f"{'edge>=' + str(e):>15}" for e in EDGES)
    print("\n1X2 vs CLOSING: log loss per match (lower = better); flat bets at the closing odds, ROI (bets)")
    print(f"  {'div':<5} {'n':>6} {'model':>7} {'market':>7} {head}")
    for k in keys:
        r = rows[k]
        if r["n"] >= 200:
            print(f"  {k:<5} {r['n']:6d} {r['ll_m'] / r['n']:7.4f} {r['ll_k'] / r['n']:7.4f} "
                  + " ".join(f"{roi(r[f'b{e}']):>15}" for e in EDGES))
    print("\n1X2 vs OPENING (main leagues): log loss model vs opening market; flat bets at the opening odds, "
          "ROI / CLV (bets)")
    print(f"  {'div':<5} {'n':>6} {'model':>7} {'open':>7} " + " ".join(f"{'edge>=' + str(e):>22}" for e in EDGES))
    for k in keys:
        r = rows[k]
        if r["n_op"] >= 200:
            cells = " ".join(f"{(f'{b[1] / b[0]:+.1%} / {b[2] / b[0]:+.1%} ({b[0]})' if b[0] else '—'):>22}"
                             for b in (r[f"op{e}"] for e in EDGES))
            print(f"  {k:<5} {r['n_op']:6d} {r['ll_m_op'] / r['n_op']:7.4f} {r['ll_k_op'] / r['n_op']:7.4f} {cells}")
    print("\nOver/under 2.5 vs CLOSING")
    print(f"  {'div':<5} {'n':>6} {'model':>7} {'market':>7} {head}")
    for k in keys:
        r = rows[k]
        if r["n_ou"] >= 200:
            print(f"  {k:<5} {r['n_ou']:6d} {r['ll_m_ou'] / r['n_ou']:7.4f} {r['ll_k_ou'] / r['n_ou']:7.4f} "
                  + " ".join(f"{roi(r[f'o{e}']):>15}" for e in EDGES))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=2012, help="first evaluated season (Pinnacle closing from 2012)")
    ap.add_argument("--divs", help="comma list of divs (default: all)")
    ap.add_argument("--xi", type=float, default=0.0019, help="time decay per day (half-life ln2/xi)")
    ap.add_argument("--shots", type=float, default=0.0, help="weight of shots on target in the strength fit (0-1)")
    ap.add_argument("--promoted", type=float, default=0.0,
                    help="prior attack -d / weakness +d of teams new to a top division")
    args = ap.parse_args()
    conn = fd.connect()
    df = load(conn, args.since, args.divs.split(",") if args.divs else None)
    print(f"{len(df)} matches loaded; walk-forward from season {args.since}, xi {args.xi} "
          f"(half-life {math.log(2) / args.xi:.0f} days), shots {args.shots}, promoted {args.promoted}", flush=True)
    preds = [walk_forward(g, args.since, args.xi, args.shots, args.promoted) for _, g in df.groupby("div")]
    evaluate(pd.concat([p for p in preds if len(p)], ignore_index=True))


if __name__ == "__main__":
    main()
