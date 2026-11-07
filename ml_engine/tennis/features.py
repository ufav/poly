"""Pre-match features of tennis singles (docs/md1.md 8.3), every value from the matches played before (no leakage):

- Elo overall and by surface (hard / clay / grass), K decreasing with the player's experience (K = 250 / (n + 5)^0.4),
  and the surface-blended probability;
- ATP / WTA entry ranking and points (tennis-data, the latest known before the match);
- form: win share of the last 10 and of the last 10 on this surface;
- fatigue: matches and games in the last 7 / 14 days, days since the last match;
- head-to-head: recency-weighted share of the earlier meetings won;
- context: level (slam / 1000 / 500 / 250 / challenger / itf / utr / other), surface, indoor, best of 5.
Retirements / walkovers do not update the ratings; doubles are not in the pool.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import defaultdict, deque

import numpy as np
import pandas as pd
from sqlalchemy import text

ELO_START = 1500.0
H2H_HL_DAYS = 730
SURFACES = ("hard", "clay", "grass")
LEVELS = ("slam", "1000", "500", "250", "challenger", "itf", "utr", "other")


def surface_of(league: str, extra: dict | None) -> tuple[str | None, bool]:
    s = (extra or {}).get("surface") if isinstance(extra, dict) else None
    court = (extra or {}).get("court") if isinstance(extra, dict) else None
    text_ = f"{league or ''} {s or ''}".lower()
    indoor = "indoor" in text_ or (court or "").lower() == "indoor"
    for surf in ("clay", "grass", "carpet", "hard"):
        if surf in text_:
            return ("hard" if surf == "carpet" else surf), indoor
    return None, indoor


def level_of(league: str, extra: dict | None) -> str:
    series = ((extra or {}).get("series") or "") if isinstance(extra, dict) else ""
    t = f"{series} {league or ''}"
    if re.search(r"Grand Slam|Australian Open|Roland Garros|Wimbledon|US Open", t):
        return "slam"
    if re.search(r"1000", t):
        return "1000"
    if re.search(r"500", t):
        return "500"
    if re.search(r"250|ATP Cup|United Cup", t):
        return "250"
    if re.search(r"Challenger|WTA 125|125K", t):
        return "challenger"
    if re.search(r"ITF|World Tennis|\bM15\b|\bM25\b|\bW\d{2,3}\b", t):
        return "itf"
    if re.search(r"UTR", t):
        return "utr"
    return "other"


def load_matches(conn, since: dt.datetime) -> pd.DataFrame:
    m = pd.read_sql(text("""
        SELECT m.id, m.start_time, m.home_team_id h, m.away_team_id a, m.home_score hs, m.away_score aws,
               m.periods, m.result_extra, m.status, l.name league, l.gender
        FROM matches m JOIN leagues l ON l.id = m.league_id
        JOIN teams th ON th.id = m.home_team_id JOIN teams ta ON ta.id = m.away_team_id
        WHERE m.sport_id = 2 AND m.start_time >= :since AND m.status IN ('finished', 'walkover')
          AND th.kind = 'player' AND ta.kind = 'player' AND l.name !~* 'doubles|mixed'"""), conn,
        params={"since": since})
    ranks = pd.read_sql(text("""
        SELECT st.match_id, st.stat, st.home_value, st.away_value FROM match_statistics st
        JOIN matches m ON m.id = st.match_id
        WHERE m.sport_id = 2 AND m.start_time >= :since AND st.source_id = 5 AND st.stat IN ('rank', 'rank_points')
          AND st.period = 0"""), conn, params={"since": since})
    for stat in ("rank", "rank_points"):
        part = ranks[ranks.stat == stat].set_index("match_id")
        m[f"h_{stat}"] = m["id"].map(part.home_value).astype(float)
        m[f"a_{stat}"] = m["id"].map(part.away_value).astype(float)
    m["start_time"] = pd.to_datetime(m["start_time"], utc=True)
    sv = [surface_of(lg, ex) for lg, ex in zip(m.league, m.result_extra)]
    m["surface"] = [x[0] for x in sv]
    m["indoor"] = [int(x[1]) for x in sv]
    m["level"] = [level_of(lg, ex) for lg, ex in zip(m.league, m.result_extra)]
    m["best_of5"] = [int(isinstance(ex, dict) and ex.get("best_of") == 5) or int(lv == "slam" and g == "m")
                     for ex, lv, g in zip(m.result_extra, m.level, m.gender)]
    m["games"] = m.periods.map(lambda p: sum(a + b for a, b in p) if isinstance(p, list) and p else np.nan)
    m["complete"] = [(st == "finished") and not (isinstance(ex, dict) and (ex.get("incomplete") or
                     ex.get("comment") in ("Retired", "Walkover", "Awarded")))
                     for st, ex in zip(m.status, m.result_extra)]
    return m.sort_values("start_time").reset_index(drop=True)


def k_factor(n: int) -> float:
    return 250.0 / (n + 5) ** 0.4


def build(m: pd.DataFrame) -> pd.DataFrame:
    elo = defaultdict(lambda: ELO_START)
    elo_s = defaultdict(lambda: ELO_START)            # (player, surface)
    n_all = defaultdict(int)
    n_s = defaultdict(int)
    last10 = defaultdict(lambda: deque(maxlen=10))
    last10_s = defaultdict(lambda: deque(maxlen=10))
    hist = defaultdict(list)                          # player -> [(time, games)]
    rank = {}                                         # player -> (rank, points)
    h2h = defaultdict(list)                           # (a, b) -> [(time, a won)]
    rows = []
    for t in m.itertuples():
        h, a, ts, surf = t.h, t.a, t.start_time, t.surface
        f = {"match_id": t.id}
        eh, ea = elo[h], elo[a]
        f.update(elo_h=eh, elo_a=ea, elo_diff=eh - ea, elo_p=1 / (1 + 10 ** ((ea - eh) / 400)),
                 n_h=n_all[h], n_a=n_all[a])
        if surf:
            sh, sa = elo_s[(h, surf)], elo_s[(a, surf)]
            wh = n_s[(h, surf)] / (n_s[(h, surf)] + 10)
            wa = n_s[(a, surf)] / (n_s[(a, surf)] + 10)
            bh, ba = wh * sh + (1 - wh) * eh, wa * sa + (1 - wa) * ea
            f.update(selo_diff=sh - sa, blend_diff=bh - ba, blend_p=1 / (1 + 10 ** ((ba - bh) / 400)),
                     ns_h=n_s[(h, surf)], ns_a=n_s[(a, surf)])
        for side, p in (("h", h), ("a", a)):
            own = getattr(t, f"{side}_rank")  # entry ranking at the start of the tournament: known before the match
            r = (own, getattr(t, f"{side}_rank_points")) if not math.isnan(own) else rank.get(p)
            f[f"{side}_rank"] = r[0] if r else np.nan
            f[f"{side}_points"] = r[1] if r else np.nan
            f[f"{side}_form10"] = np.mean(last10[p]) if last10[p] else np.nan
            f[f"{side}_form10_s"] = np.mean(last10_s[(p, surf)]) if surf and last10_s[(p, surf)] else np.nan
            past = hist[p]
            f[f"{side}_m7"] = sum(1 for x, _ in past[-12:] if (ts - x).days < 7)
            f[f"{side}_m14"] = sum(1 for x, _ in past[-20:] if (ts - x).days < 14)
            f[f"{side}_g7"] = sum(g for x, g in past[-12:] if (ts - x).days < 7 and g == g)
            f[f"{side}_rest"] = min((ts - past[-1][0]).total_seconds() / 86400, 60) if past else np.nan
        if not math.isnan(f["h_rank"]) and not math.isnan(f["a_rank"]):
            f["log_rank_ratio"] = math.log(f["a_rank"] / f["h_rank"])
        key = (min(h, a), max(h, a))
        ws = [(0.5 ** ((ts - x).days / H2H_HL_DAYS), won if h == key[0] else 1 - won) for x, won in h2h[key]]
        f["h2h_n"] = len(ws)
        f["h2h_share"] = sum(w * v for w, v in ws) / sum(w for w, _ in ws) if ws else np.nan
        f["surface"] = surf or "unknown"
        f["level"] = t.level
        f["indoor"] = t.indoor
        f["best_of5"] = t.best_of5
        rows.append(f)
        # ---- update ----
        for side, p in (("h", h), ("a", a)):
            if not math.isnan(getattr(t, f"{side}_rank")):
                rank[p] = (getattr(t, f"{side}_rank"), getattr(t, f"{side}_rank_points"))
        if t.status == "walkover":
            continue
        hist[h].append((ts, t.games))
        hist[a].append((ts, t.games))
        if not t.complete or t.hs is None or t.hs == t.aws:
            continue
        won = 1.0 if t.hs > t.aws else 0.0
        exp = 1 / (1 + 10 ** ((ea - eh) / 400))
        elo[h] += k_factor(n_all[h]) * (won - exp)
        elo[a] -= k_factor(n_all[a]) * (won - exp)
        n_all[h] += 1
        n_all[a] += 1
        if surf:
            sh, sa = elo_s[(h, surf)], elo_s[(a, surf)]
            exp_s = 1 / (1 + 10 ** ((sa - sh) / 400))
            elo_s[(h, surf)] += k_factor(n_s[(h, surf)]) * (won - exp_s)
            elo_s[(a, surf)] -= k_factor(n_s[(a, surf)]) * (won - exp_s)
            n_s[(h, surf)] += 1
            n_s[(a, surf)] += 1
            last10_s[(h, surf)].append(won)
            last10_s[(a, surf)].append(1 - won)
        last10[h].append(won)
        last10[a].append(1 - won)
        h2h[key].append((ts, won if h == key[0] else 1 - won))
    feats = pd.DataFrame(rows)
    for k in ("form10", "form10_s", "m7", "m14", "g7", "rest", "points"):
        feats[f"d_{k}"] = feats[f"h_{k}"] - feats[f"a_{k}"]
    return feats
