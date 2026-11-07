"""Pre-match features of football matches (docs/md1.md 8.2), every value computed only from the matches played
before kick-off (no leakage):

- Elo (home advantage, goal-difference multiplier) and the Dixon-Coles rates of the weekly refit;
- form: exponentially weighted means (half-life FORM_HL games) of goals, shots, shots on target, corners for and
  against; points over the Elo expectation (form adjusted for the opponents' strength); home form at home, away
  form away; points of the last 5;
- schedule: rest days, games in the last 14 days, the next game within 4 days (rotation risk);
- motivation: league table of the current season before the match - position (0 = top, 1 = bottom), points per
  game, season progress;
- head-to-head: recency-weighted goal difference of earlier meetings;
- competition: league / cup / UEFA cup.
Match statistics of different sources are unified (shots = shots / total_shots, corners = corners / won_corners;
football-data first, then ESPN, then Olimpbet).
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict, deque

import numpy as np
import pandas as pd
from sqlalchemy import text

FORM_HL = 8.0                 # games
ELO_K, ELO_HFA, ELO_START = 20.0, 60.0, 1500.0
H2H_HL_DAYS = 730
STATS = {"shots": ("shots", "total_shots"), "sot": ("shots_on_target",), "corners": ("corners", "won_corners")}
SOURCE_RANK = {1: 0, 9: 1, 3: 2}
CALENDAR_YEAR = {"Brazil", "USA", "Japan", "China", "Norway", "Sweden", "Finland", "Ireland", "Argentina",
                 "South Korea", "Iceland", "Kazakhstan", "Belarus", "Estonia", "Latvia",
                 "Lithuania", "Faroe Islands", "Uzbekistan"}
UEFA_CUPS = ("Лига Чемпионов УЕФА", "Лига Европы УЕФА", "Лига Конференций УЕФА")


def load_matches(conn, since: dt.datetime) -> pd.DataFrame:
    """Pool matches (ml_engine.football.dixon_coles.load_pool) since `since` with their league and stats."""
    from ml_engine.football.dixon_coles import load_pool
    days = (dt.datetime.now(dt.timezone.utc) - since).days + 1
    m = load_pool(conn, dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1), days=days)
    leagues = pd.read_sql(text("SELECT id league_id, country, name league, kind FROM leagues WHERE sport_id = 1"), conn)
    m = m.merge(leagues, on="league_id", how="left")
    st = pd.read_sql(text("""
        SELECT st.match_id, st.source_id, st.stat, st.home_value, st.away_value FROM match_statistics st
        WHERE st.period = 0 AND st.match_id = ANY(:ids)
          AND st.stat IN ('shots', 'total_shots', 'shots_on_target', 'corners', 'won_corners')"""), conn,
        params={"ids": m["id"].astype(int).tolist()})
    st["key"] = st.stat.map({v: k for k, vs in STATS.items() for v in vs})
    st["rank"] = st.source_id.map(SOURCE_RANK).fillna(9)
    st = st.sort_values("rank").drop_duplicates(["match_id", "key"])
    for k in STATS:
        part = st[st.key == k].set_index("match_id")
        m[f"h_{k}"] = m["id"].map(part.home_value).astype(float)
        m[f"a_{k}"] = m["id"].map(part.away_value).astype(float)
    m["start_time"] = pd.to_datetime(m["start_time"], utc=True)
    return m.sort_values("start_time").reset_index(drop=True)


def season_key(league_id: int, country: str | None, t: pd.Timestamp) -> tuple:
    if country in CALENDAR_YEAR:
        return league_id, t.year
    return league_id, t.year if t.month >= 7 else t.year - 1


class Ewm:
    """Exponentially weighted mean over games (NaN values skipped)."""

    __slots__ = ("s", "w")

    def __init__(self) -> None:
        self.s, self.w = 0.0, 0.0

    def add(self, x: float) -> None:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return
        a = 0.5 ** (1 / FORM_HL)
        self.s, self.w = self.s * a + x, self.w * a + 1

    @property
    def value(self) -> float:
        return self.s / self.w if self.w > 0.5 else np.nan


def build(m: pd.DataFrame) -> pd.DataFrame:
    """One feature row per match (pre-match values), chronological pass over the pool."""
    elo = defaultdict(lambda: ELO_START)
    form = defaultdict(lambda: defaultdict(Ewm))       # team -> metric -> Ewm
    last5 = defaultdict(lambda: deque(maxlen=5))
    dates = defaultdict(list)                          # team -> kick-off times
    table = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # season -> team -> [points, games]
    h2h = defaultdict(list)                            # (a, b) sorted -> [(time, gd from a's side)]
    for t in m.itertuples():
        dates[t.h].append(t.start_time)
        dates[t.a].append(t.start_time)
    for d in dates.values():
        d.sort()
    pos = {k: 0 for k in dates}
    is_uefa = m.league.isin(UEFA_CUPS).to_numpy()
    rows = []
    for k, t in enumerate(m.itertuples()):
        h, a, ts = t.h, t.a, t.start_time
        f = {"match_id": t.id}
        eh, ea = elo[h], elo[a]
        exp_h = 1 / (1 + 10 ** (-(eh + ELO_HFA - ea) / 400))
        f.update(elo_h=eh, elo_a=ea, elo_diff=eh + ELO_HFA - ea, elo_exp=exp_h)
        for side, team in (("h", h), ("a", a)):
            fm = form[team]
            for key in ("gf", "ga", "shots_f", "shots_a", "sot_f", "sot_a", "corners_f", "corners_a", "pts_over_exp"):
                f[f"{side}_{key}"] = fm[key].value
            f[f"{side}_venue_pts"] = fm["home_pts" if side == "h" else "away_pts"].value
            f[f"{side}_last5_ppg"] = np.mean(last5[team]) if last5[team] else np.nan
            # schedule: rest days, games in 14 days, next game within 4 days
            ds = dates[team]
            i = pos[team]
            while i < len(ds) and ds[i] < ts:
                i += 1
            pos[team] = i
            prev = ds[i - 1] if i > 0 else None
            f[f"{side}_rest"] = min((ts - prev).total_seconds() / 86400, 30) if prev is not None else np.nan
            f[f"{side}_games14"] = sum(1 for x in ds[max(0, i - 8):i] if (ts - x).days <= 14)
            nxt = next((x for x in ds[i + 1:i + 3] if x > ts), None)
            f[f"{side}_next4"] = int(nxt is not None and (nxt - ts).total_seconds() / 86400 <= 4)
        # league table of the season so far
        cup = t.kind in ("cup", "international") or is_uefa[k]
        f["is_cup"], f["is_uefa"] = int(cup), int(is_uefa[k])
        if not cup:
            sk = season_key(t.league_id, t.country, ts)
            tab = table[sk]
            ranked = sorted(tab.items(), key=lambda x: -(x[1][0] / max(x[1][1], 1)))
            n = len(ranked)
            rank = {team: r for r, (team, _) in enumerate(ranked)}
            for side, team in (("h", h), ("a", a)):
                pts, g = tab[team] if team in tab else (0, 0)
                f[f"{side}_pos"] = rank[team] / max(n - 1, 1) if team in rank and n > 1 else np.nan
                f[f"{side}_ppg_season"] = pts / g if g else np.nan
            games = [v[1] for v in tab.values()]
            f["season_progress"] = (np.median(games) / max(2 * (n - 1), 1)) if games and n > 1 else 0.0
        # head-to-head
        key = (min(h, a), max(h, a))
        sgn = 1 if h == key[0] else -1
        ws = [(0.5 ** ((ts - x).days / H2H_HL_DAYS), gd * sgn) for x, gd in h2h[key] if (ts - x).days <= 1500]
        f["h2h_n"] = len(ws)
        f["h2h_gd"] = sum(w * g for w, g in ws) / sum(w for w, _ in ws) if ws else np.nan
        rows.append(f)
        # ---- update with the result ----
        hg, ag = t.hg, t.ag
        res = 1.0 if hg > ag else 0.5 if hg == ag else 0.0
        mult = math.log(abs(hg - ag) + 1) + 1 if hg != ag else 1.0
        delta = ELO_K * mult * (res - exp_h)
        elo[h], elo[a] = eh + delta, ea - delta
        ph, pa = (3, 0) if hg > ag else (1, 1) if hg == ag else (0, 3)
        for team, gf, ga, sf, sa, of, oa, cf, ca, pts, exp in (
                (h, hg, ag, t.h_shots, t.a_shots, t.h_sot, t.a_sot, t.h_corners, t.a_corners, ph, exp_h),
                (a, ag, hg, t.a_shots, t.h_shots, t.a_sot, t.h_sot, t.a_corners, t.h_corners, pa, 1 - exp_h)):
            fm = form[team]
            for key, x in (("gf", gf), ("ga", ga), ("shots_f", sf), ("shots_a", sa), ("sot_f", of), ("sot_a", oa),
                           ("corners_f", cf), ("corners_a", ca)):
                fm[key].add(float(x) if x is not None else np.nan)
            fm["pts_over_exp"].add(pts / 3 - exp)
            fm["home_pts" if team == h else "away_pts"].add(pts / 3)
            last5[team].append(pts)
        if not cup:
            tab = table[season_key(t.league_id, t.country, ts)]
            tab[h][0] += ph
            tab[h][1] += 1
            tab[a][0] += pa
            tab[a][1] += 1
        h2h[key].append((ts, (hg - ag) if h == key[0] else (ag - hg)))
    feats = pd.DataFrame(rows)
    for k in ("gf", "ga", "shots_f", "shots_a", "sot_f", "sot_a", "corners_f", "corners_a", "pts_over_exp",
              "venue_pts", "last5_ppg", "rest", "games14"):
        feats[f"d_{k}"] = feats[f"h_{k}"] - feats[f"a_{k}"]
    return feats
