"""Pre-match features of NBA games (docs/md1.md 8.4), every value from the games before tip-off:

- Elo with the margin of victory and home court (FiveThirtyEight-style);
- the four factors and ratings as exponentially weighted means (half-life FORM_HL games), for and against:
  offensive / defensive rating (points per 100 possessions), pace, eFG%, turnover %, offensive rebound %, FT rate;
- form: season win share, last-10 win share;
- schedule: rest days, back-to-back, games in the last 7 days;
- absences: the share of the usual minutes of the team's top 8 (weighted mean minutes of its recent games) that
  sits out - on the history from the lineups (who played), for an upcoming game from the injury report (Out).
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict, deque

import numpy as np
import pandas as pd
from sqlalchemy import text

FORM_HL = 10.0
ELO_K, ELO_HFA, ELO_START = 20.0, 100.0, 1500.0
STATS = {"fgm": "field_goals_made", "fga": "field_goals_attempted", "tpm": "three_point_field_goals_made",
         "fta": "free_throws_attempted", "ftm": "free_throws_made", "oreb": "offensive_rebounds",
         "dreb": "defensive_rebounds", "tov": "total_turnovers"}
OUT_STATUS = ("Out", "Injured Reserve", "Suspension")


def league_ids(conn, name: str = "NBA") -> list[int]:
    return conn.execute(text("SELECT id FROM leagues WHERE sport_id = 3 AND name = :n AND country = 'USA'"),
                        {"n": name}).scalars().all()


def load(conn, since: dt.datetime, league: str = "NBA", upcoming: list[int] | None = None) -> pd.DataFrame:
    lids = league_ids(conn, league)
    m = pd.read_sql(text("""
        SELECT m.id, m.start_time, m.home_team_id h, m.away_team_id a, m.home_score hs, m.away_score aws, m.status
        FROM matches m WHERE m.league_id = ANY(:l) AND m.start_time >= :since
          AND (m.status = 'finished' OR m.id = ANY(:up))"""), conn,
        params={"l": lids, "since": since, "up": list(upcoming or [])})
    ids = m.id.astype(int).tolist()
    st = pd.read_sql(text("""SELECT match_id, stat, home_value, away_value FROM match_statistics
        WHERE source_id = 9 AND period = 0 AND match_id = ANY(:ids) AND stat = ANY(:s)"""), conn,
        params={"ids": ids, "s": list(STATS.values())})
    for k, v in STATS.items():
        part = st[st.stat == v].set_index("match_id")
        m[f"h_{k}"] = m.id.map(part.home_value).astype(float)
        m[f"a_{k}"] = m.id.map(part.away_value).astype(float)
    pl = pd.read_sql(text("""SELECT match_id, team_id, player_id, minutes FROM match_players
        WHERE source_id = 9 AND match_id = ANY(:ids) AND minutes > 0"""), conn, params={"ids": ids})
    played = defaultdict(dict)
    for r in pl.itertuples():
        played[r.match_id].setdefault(r.team_id, {})[r.player_id] = float(r.minutes)
    m["played"] = m.id.map(lambda i: played.get(i, {}))
    m["start_time"] = pd.to_datetime(m.start_time, utc=True)
    return m.sort_values("start_time").reset_index(drop=True)


def injured_out(conn, team_ids: list[int], at: dt.datetime) -> dict[int, set[int]]:
    """team -> players whose latest injury report status before `at` is Out (for an upcoming game)."""
    rows = conn.execute(text("""
        SELECT DISTINCT ON (player_id) player_id, team_id, status FROM injuries
        WHERE team_id = ANY(:t) AND fetched_at <= :at ORDER BY player_id, fetched_at DESC"""),
        {"t": team_ids, "at": at}).all()
    out = defaultdict(set)
    for pid, tid, status in rows:
        if status in OUT_STATUS:
            out[tid].add(pid)
    return out


class Ewm:
    __slots__ = ("s", "w")

    def __init__(self) -> None:
        self.s, self.w = 0.0, 0.0

    def add(self, x: float) -> None:
        if x is None or math.isnan(x):
            return
        a = 0.5 ** (1 / FORM_HL)
        self.s, self.w = self.s * a + x, self.w * a + 1

    @property
    def value(self) -> float:
        return self.s / self.w if self.w > 0.5 else np.nan


def season_of(t: pd.Timestamp) -> int:
    return t.year if t.month >= 9 else t.year - 1


def build(m: pd.DataFrame, out_now: dict[int, set[int]] | None = None) -> pd.DataFrame:
    """`out_now`: for the upcoming games, team -> players out (injury report); history uses who played."""
    elo = defaultdict(lambda: ELO_START)
    form = defaultdict(lambda: defaultdict(Ewm))
    last10 = defaultdict(lambda: deque(maxlen=10))
    season = defaultdict(lambda: [0, 0])                   # (season, team) -> [wins, games]
    dates = defaultdict(list)
    mins = defaultdict(lambda: defaultdict(Ewm))           # team -> player -> minutes Ewm
    recent = defaultdict(lambda: deque(maxlen=15))         # team -> last games' player sets
    rows = []
    for t in m.itertuples():
        h, a, ts = t.h, t.a, t.start_time
        f = {"match_id": t.id}
        eh, ea = elo[h], elo[a]
        f.update(elo_diff=eh + ELO_HFA - ea, elo_p=1 / (1 + 10 ** (-(eh + ELO_HFA - ea) / 400)))
        for side, team in (("h", h), ("a", a)):
            fm = form[team]
            for k in ("ortg", "drtg", "pace", "efg", "tov_pct", "oreb_pct", "ftr", "efg_a", "tov_pct_a", "oreb_pct_a"):
                f[f"{side}_{k}"] = fm[k].value
            f[f"{side}_last10"] = np.mean(last10[team]) if last10[team] else np.nan
            w, g = season[(season_of(ts), team)]
            f[f"{side}_season_wp"] = w / g if g else np.nan
            f[f"{side}_games"] = g
            prev = [x for x in dates[team] if x < ts]
            f[f"{side}_rest"] = min((ts - prev[-1]).total_seconds() / 86400, 10) if prev else np.nan
            f[f"{side}_b2b"] = int(bool(prev) and (ts - prev[-1]).total_seconds() < 36 * 3600)
            f[f"{side}_g7"] = sum(1 for x in prev[-6:] if (ts - x).days < 7)
            # absences: usual minutes of the top 8 of the recent games that sit out
            pool = {p for s in recent[team] for p in s}
            usual = sorted(((mins[team][p].value, p) for p in pool if not math.isnan(mins[team][p].value)),
                           reverse=True)[:8]
            total = sum(v for v, _ in usual)
            if t.status == "finished":
                active = set((t.played or {}).get(team, {}))
                missing = [v for v, p in usual if p not in active] if active else []
            else:
                gone = (out_now or {}).get(team, set())
                missing = [v for v, p in usual if p in gone]
            f[f"{side}_miss_share"] = sum(missing) / total if total > 0 and (t.status != "finished" or
                                                                           (t.played or {}).get(team)) else np.nan
            f[f"{side}_miss_top"] = max(missing) if missing else 0.0
        rows.append(f)
        if t.status != "finished" or t.hs is None:
            continue
        # ---- update ----
        hs, aws = float(t.hs), float(t.aws)
        mov = hs - aws
        won = 1.0 if mov > 0 else 0.0
        exp = 1 / (1 + 10 ** (-(eh + ELO_HFA - ea) / 400))
        diff = (eh + ELO_HFA - ea) if won else (ea - eh - ELO_HFA)
        mult = (abs(mov) + 3) ** 0.8 / (7.5 + 0.006 * diff)
        elo[h], elo[a] = eh + ELO_K * mult * (won - exp), ea - ELO_K * mult * (won - exp)
        if not math.isnan(t.h_fga) and not math.isnan(t.a_fga):
            poss_h = t.h_fga - t.h_oreb + t.h_tov + 0.44 * t.h_fta
            poss_a = t.a_fga - t.a_oreb + t.a_tov + 0.44 * t.a_fta
            poss = (poss_h + poss_a) / 2
            for team, pf, pa_, s, o in ((h, hs, aws, "h", "a"), (a, aws, hs, "a", "h")):
                g = lambda k, side=s: getattr(t, f"{side}_{k}")
                go = lambda k, side=o: getattr(t, f"{side}_{k}")
                fm = form[team]
                fm["ortg"].add(100 * pf / poss)
                fm["drtg"].add(100 * pa_ / poss)
                fm["pace"].add(poss)
                fm["efg"].add((g("fgm") + 0.5 * g("tpm")) / g("fga"))
                fm["tov_pct"].add(g("tov") / (g("fga") + 0.44 * g("fta") + g("tov")))
                fm["oreb_pct"].add(g("oreb") / (g("oreb") + go("dreb")))
                fm["ftr"].add(g("ftm") / g("fga"))
                fm["efg_a"].add((go("fgm") + 0.5 * go("tpm")) / go("fga"))
                fm["tov_pct_a"].add(go("tov") / (go("fga") + 0.44 * go("fta") + go("tov")))
                fm["oreb_pct_a"].add(go("oreb") / (go("oreb") + g("dreb")))
        for team, res in ((h, won), (a, 1 - won)):
            last10[team].append(res)
            sk = (season_of(ts), team)
            season[sk][0] += res
            season[sk][1] += 1
            dates[team].append(ts)
            played = (t.played or {}).get(team, {})
            if played:
                recent[team].append(set(played))
                for p, mn in played.items():
                    mins[team][p].add(mn)
                for p in {q for s in recent[team] for q in s} - set(played):
                    mins[team][p].add(0.0)
    feats = pd.DataFrame(rows)
    for k in ("ortg", "drtg", "pace", "efg", "tov_pct", "oreb_pct", "ftr", "efg_a", "last10", "season_wp", "rest",
              "g7", "miss_share", "miss_top"):
        feats[f"d_{k}"] = feats[f"h_{k}"] - feats[f"a_{k}"]
    feats["d_net"] = (feats.h_ortg - feats.h_drtg) - (feats.a_ortg - feats.a_drtg)
    return feats
