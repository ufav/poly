"""Pre-match features of NHL games (docs/md1.md 8.6), every value from the games before:

- Elo with the goal margin and home ice;
- team form as exponentially weighted means (FORM_HL games): goals and shots for / against, power play %, penalty
  kill %, faceoff %, shooting % and save %;
- the starting goalie: his weighted save % over his recent starts, his number of starts, whether he is the team's
  usual starter (history: the goalie with the most ice time; an upcoming game: the usual starter unless he is out
  on the injury report);
- absences: the share of the usual ice time of the top 12 skaters that sits out;
- schedule: rest days, back-to-back, games in 7 days; season / last-10 win share.
Results include overtime / shootout (the Polymarket moneyline does too).
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter, defaultdict, deque

import numpy as np
import pandas as pd
from sqlalchemy import text

from ml_engine.basketball.features import OUT_STATUS, Ewm, injured_out  # noqa: F401  (re-exported)

FORM_HL = 10.0
GOALIE_HL = 15.0
ELO_K, ELO_HFA, ELO_START = 10.0, 35.0, 1500.0
STATS = {"shots": "shots_total", "ppg": "power_play_goals", "ppo": "power_play_opportunities",
         "fow": "faceoffs_won"}
LEAGUE_SV = 0.900


def league_ids(conn) -> list[int]:
    return conn.execute(text("SELECT id FROM leagues WHERE sport_id = 5 AND name = 'NHL'")).scalars().all()


def toi(v) -> float:
    try:
        mm, ss = str(v).split(":")[:2]
        return int(mm) + int(ss) / 60
    except (ValueError, AttributeError):
        return 0.0


def load(conn, since: dt.datetime, upcoming: list[int] | None = None) -> pd.DataFrame:
    m = pd.read_sql(text("""
        SELECT m.id, m.start_time, m.home_team_id h, m.away_team_id a, m.home_score hs, m.away_score aws, m.status
        FROM matches m WHERE m.league_id = ANY(:l) AND m.start_time >= :since
          AND (m.status = 'finished' OR m.id = ANY(:up))"""), conn,
        params={"l": league_ids(conn), "since": since, "up": list(upcoming or [])})
    ids = m.id.astype(int).tolist()
    st = pd.read_sql(text("""SELECT match_id, stat, home_value, away_value FROM match_statistics
        WHERE source_id = 9 AND period = 0 AND match_id = ANY(:ids) AND stat = ANY(:s)"""), conn,
        params={"ids": ids, "s": list(STATS.values())})
    for k, v in STATS.items():
        part = st[st.stat == v].set_index("match_id")
        m[f"h_{k}"] = m.id.map(part.home_value).astype(float)
        m[f"a_{k}"] = m.id.map(part.away_value).astype(float)
    pl = pd.read_sql(text("""SELECT match_id, team_id, player_id, position, minutes, stats->>'SA' sa,
        stats->>'GA' ga FROM match_players WHERE source_id = 9 AND match_id = ANY(:ids) AND minutes > 0"""), conn,
        params={"ids": ids})
    skaters, goalies = defaultdict(dict), defaultdict(dict)
    for r in pl.itertuples():
        if r.position == "G":
            goalies[r.match_id].setdefault(r.team_id, []).append((float(r.minutes), r.player_id,
                                                                 float(r.sa or 0), float(r.ga or 0)))
        else:
            skaters[r.match_id].setdefault(r.team_id, {})[r.player_id] = float(r.minutes)
    m["skaters"] = m.id.map(lambda i: skaters.get(i, {}))
    m["goalies"] = m.id.map(lambda i: goalies.get(i, {}))
    m["start_time"] = pd.to_datetime(m.start_time, utc=True)
    return m.sort_values("start_time").reset_index(drop=True)


def season_of(t: pd.Timestamp) -> int:
    return t.year if t.month >= 9 else t.year - 1


def build(m: pd.DataFrame, out_now: dict[int, set[int]] | None = None) -> pd.DataFrame:
    elo = defaultdict(lambda: ELO_START)
    form = defaultdict(lambda: defaultdict(Ewm))
    last10 = defaultdict(lambda: deque(maxlen=10))
    season = defaultdict(lambda: [0, 0])
    dates = defaultdict(list)
    g_sv = defaultdict(lambda: [0.0, 0.0])              # goalie -> weighted [saves, shots]
    g_starts = Counter()
    starters = defaultdict(lambda: deque(maxlen=10))   # team -> recent starting goalies
    mins = defaultdict(lambda: defaultdict(Ewm))
    recent = defaultdict(lambda: deque(maxlen=15))
    decay = 0.5 ** (1 / GOALIE_HL)
    rows = []
    for t in m.itertuples():
        h, a, ts = t.h, t.a, t.start_time
        f = {"match_id": t.id}
        eh, ea = elo[h], elo[a]
        f.update(elo_diff=eh + ELO_HFA - ea, elo_p=1 / (1 + 10 ** (-(eh + ELO_HFA - ea) / 400)))
        for side, team in (("h", h), ("a", a)):
            fm = form[team]
            for k in ("gf", "ga", "sf", "sa", "pp", "pk", "fo", "sh_pct", "sv_pct"):
                f[f"{side}_{k}"] = fm[k].value
            f[f"{side}_last10"] = np.mean(last10[team]) if last10[team] else np.nan
            w, g = season[(season_of(ts), team)]
            f[f"{side}_season_wp"] = w / g if g else np.nan
            prev = dates[team]
            f[f"{side}_rest"] = min((ts - prev[-1]).total_seconds() / 86400, 10) if prev else np.nan
            f[f"{side}_b2b"] = int(bool(prev) and (ts - prev[-1]).total_seconds() < 36 * 3600)
            f[f"{side}_g7"] = sum(1 for x in prev[-5:] if (ts - x).days < 7)
            # the starting goalie
            usual = Counter(starters[team]).most_common(1)
            usual_g = usual[0][0] if usual else None
            if t.status == "finished":
                gl = (t.goalies or {}).get(team) or []
                starter = max(gl)[1] if gl else None
            else:
                out = (out_now or {}).get(team, set())
                starter = usual_g if usual_g not in out else next(
                    (gk for gk, _ in Counter(starters[team]).most_common() if gk not in out), None)
            sv, shots = g_sv[starter] if starter else (0.0, 0.0)
            f[f"{side}_g_sv"] = (sv + LEAGUE_SV * 200) / (shots + 200) if starter else np.nan  # shrunk to the league
            f[f"{side}_g_starts"] = g_starts[starter] if starter else 0
            f[f"{side}_g_usual"] = int(starter is not None and starter == usual_g)
            # skater absences
            pool = {p for s in recent[team] for p in s}
            usual_sk = sorted(((mins[team][p].value, p) for p in pool if not math.isnan(mins[team][p].value)),
                              reverse=True)[:12]
            total = sum(v for v, _ in usual_sk)
            if t.status == "finished":
                active = set((t.skaters or {}).get(team, {}))
                missing = [v for v, p in usual_sk if p not in active] if active else []
                known = bool(active)
            else:
                gone = (out_now or {}).get(team, set())
                missing = [v for v, p in usual_sk if p in gone]
                known = True
            f[f"{side}_miss_share"] = sum(missing) / total if total > 0 and known else np.nan
        rows.append(f)
        if t.status != "finished" or t.hs is None:
            continue
        hs, aws = float(t.hs), float(t.aws)
        won = 1.0 if hs > aws else 0.0
        exp = 1 / (1 + 10 ** (-(eh + ELO_HFA - ea) / 400))
        mult = math.log(abs(hs - aws) + 1) + 1
        elo[h], elo[a] = eh + ELO_K * mult * (won - exp), ea - ELO_K * mult * (won - exp)
        for team, gf, ga, s, o in ((h, hs, aws, "h", "a"), (a, aws, hs, "a", "h")):
            fm = form[team]
            fm["gf"].add(gf)
            fm["ga"].add(ga)
            sf, sa = getattr(t, f"{s}_shots"), getattr(t, f"{o}_shots")
            fm["sf"].add(sf)
            fm["sa"].add(sa)
            if sf == sf and sf > 0:
                fm["sh_pct"].add(gf / sf)
            if sa == sa and sa > 0:
                fm["sv_pct"].add(1 - ga / sa)
            ppo, ppg = getattr(t, f"{s}_ppo"), getattr(t, f"{s}_ppg")
            if ppo == ppo and ppo > 0:
                fm["pp"].add(ppg / ppo)
            oppo, oppg = getattr(t, f"{o}_ppo"), getattr(t, f"{o}_ppg")
            if oppo == oppo and oppo > 0:
                fm["pk"].add(1 - oppg / oppo)
            fw, ofw = getattr(t, f"{s}_fow"), getattr(t, f"{o}_fow")
            if fw == fw and ofw == ofw and fw + ofw > 0:
                fm["fo"].add(fw / (fw + ofw))
            last10[team].append(won if team == h else 1 - won)
            sk = (season_of(ts), team)
            season[sk][0] += won if team == h else 1 - won
            season[sk][1] += 1
            dates[team].append(ts)
            gl = (t.goalies or {}).get(team) or []
            if gl:
                starters[team].append(max(gl)[1])
                g_starts[max(gl)[1]] += 1
            for _, gid, gsa, gga in gl:
                svs, sh = g_sv[gid]
                g_sv[gid] = [svs * decay + (gsa - gga), sh * decay + gsa]
            played = (t.skaters or {}).get(team, {})
            if played:
                recent[team].append(set(played))
                for p, mn in played.items():
                    mins[team][p].add(mn)
                for p in {q for s_ in recent[team] for q in s_} - set(played):
                    mins[team][p].add(0.0)
    feats = pd.DataFrame(rows)
    for k in ("gf", "ga", "sf", "sa", "pp", "pk", "fo", "sh_pct", "sv_pct", "last10", "season_wp", "rest", "g7",
              "g_sv", "g_starts", "miss_share"):
        feats[f"d_{k}"] = feats[f"h_{k}"] - feats[f"a_{k}"]
    feats["d_shot_share"] = feats.h_sf / (feats.h_sf + feats.h_sa) - feats.a_sf / (feats.a_sf + feats.a_sa)
    return feats
