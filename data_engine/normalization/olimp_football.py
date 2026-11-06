"""Olimp archive football (data/football.sqlite ol_matches, Russian names, Almaty time) <-> football-data.co.uk teams and
leagues already in PostgreSQL, by score + date voting:

1. tournament -> league: an Olimp tournament of a football-data country (senior men) gets the football-data league whose
   matches have the same score within +-1 day most often (share >= MIN_TOURNAMENT_SHARE of its matches);
2. team -> team: inside each accepted (tournament, league) pair the candidates are near-unique; an Olimp team name gets
   the football-data team it is paired with most often (>= MIN_VOTES matches, share >= MIN_TEAM_SHARE);
3. writes source_entities (source olimp_archive): league external_id = tournament, team external_id = 'country|name',
   method 'vote', confidence = share. Existing vote mappings are replaced; manual ones are kept.

  python -m data_engine.normalization.olimp_football [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter, defaultdict
from decimal import Decimal

import pandas as pd
from sqlalchemy import delete, select

from core.db import session
from core.models import SourceEntity
from pmbot import footballdata as fd

SOURCE = 2          # olimp_archive
FD_SOURCE = 1
MIN_TOURNAMENT_SHARE = 0.7   # 0.6 let coincidences through (FA Trophy -> Championship)
MIN_TEAM_SHARE = 0.75
MIN_VOTES = 3
MIN_TOURNAMENT_MATCHES = 20

# Olimp country (tournament part 2) -> football-data country
COUNTRY = {
    "Англия": "England", "Шотландия": "Scotland", "Германия": "Germany", "Италия": "Italy", "Испания": "Spain",
    "Франция": "France", "Нидерланды": "Netherlands", "Голландия": "Netherlands", "Бельгия": "Belgium",
    "Португалия": "Portugal", "Турция": "Turkey", "Греция": "Greece", "Аргентина": "Argentina", "Австрия": "Austria",
    "Бразилия": "Brazil", "Китай": "China", "Дания": "Denmark", "Финляндия": "Finland", "Ирландия": "Ireland",
    "Япония": "Japan", "Мексика": "Mexico", "Норвегия": "Norway", "Польша": "Poland", "Румыния": "Romania",
    "Россия": "Russia", "Швеция": "Sweden", "Швейцария": "Switzerland", "США": "USA",
}
SKIP_WORDS = ("Женщины", "до 1", "до 2", "U1", "U2", "Молод", "Резерв", "Дубл", "Статистика", "Специальные")


def olimp_frame(path: str) -> pd.DataFrame:
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    ol = pd.read_sql_query("SELECT dt, tournament, home, away, fthg, ftag FROM ol_matches WHERE fthg IS NOT NULL", src)
    parts = ol["tournament"].str.split(". ", n=2, regex=False)
    ol["country"] = parts.str[1].map(COUNTRY)
    ol = ol[ol["country"].notna() & ~ol["tournament"].str.contains("|".join(SKIP_WORDS), regex=True)]
    ol = ol[~ol["home"].str.contains(r"\(", regex=True)]  # '(жен)', '(19)', '(дубль)' teams
    local = pd.to_datetime(ol["dt"]).dt.tz_localize("Asia/Almaty", ambiguous="NaT", nonexistent="NaT")
    ol["date"] = local.dt.tz_convert("UTC").dt.date
    return ol.dropna(subset=["date"]).reset_index(drop=True)


def fd_frame(path: str) -> pd.DataFrame:
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    df = pd.read_sql_query("SELECT div, date, home, away, fthg, ftag FROM fd_matches", src)
    df["country"] = df["div"].map(lambda d: (fd.MAIN_DIVS.get(d) or (EXTRA.get(d),))[0])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


EXTRA = {"ARG": "Argentina", "AUT": "Austria", "BRA": "Brazil", "CHN": "China", "DNK": "Denmark", "FIN": "Finland",
         "IRL": "Ireland", "JPN": "Japan", "MEX": "Mexico", "NOR": "Norway", "POL": "Poland", "ROU": "Romania",
         "RUS": "Russia", "SWE": "Sweden", "SWZ": "Switzerland", "USA": "USA"}


def candidates(ol: pd.DataFrame, fdf: pd.DataFrame) -> pd.DataFrame:
    """Every (Olimp match, football-data match) of the same country and score within +-1 day."""
    out = []
    for shift in (-1, 0, 1):
        f = fdf.copy()
        f["date"] = f["date"] + pd.Timedelta(days=shift)
        out.append(ol.reset_index().merge(f, on=["country", "date", "fthg", "ftag"], suffixes=("_ol", "_fd")))
    return pd.concat(out, ignore_index=True)


def map_tournaments(cand: pd.DataFrame, ol: pd.DataFrame) -> dict[str, tuple[str, float]]:
    """tournament -> (div, share of its matches that have a candidate in that div)."""
    sizes = ol.groupby("tournament").size()
    votes = cand.drop_duplicates(["index", "div"]).groupby(["tournament", "div"]).size()
    out = {}
    for t, g in votes.groupby(level=0):
        div_votes = g.droplevel(0).sort_values(ascending=False)
        share = div_votes.iloc[0] / sizes[t]
        if sizes[t] >= MIN_TOURNAMENT_MATCHES and share >= MIN_TOURNAMENT_SHARE:
            out[t] = (div_votes.index[0], float(share))
    return out


def map_teams(cand: pd.DataFrame, tmap: dict[str, tuple[str, float]]) -> dict[tuple[str, str], tuple[str, float, int]]:
    """(country, Olimp name) -> (football-data name, share, votes), within the accepted tournament -> div pairs."""
    ok = cand[cand["tournament"].map(lambda t: tmap.get(t, (None,))[0]) == cand["div"]]
    # an Olimp match with several candidates in the league on the same day / score is ambiguous: skip it
    ok = ok[ok.groupby("index")["index"].transform("size") == 1]
    votes: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for r in ok.itertuples():
        votes[(r.country, r.home_ol)][r.home_fd] += 1
        votes[(r.country, r.away_ol)][r.away_fd] += 1
    out = {}
    for key, cnt in votes.items():
        name, n = cnt.most_common(1)[0]
        total = sum(cnt.values())
        if n >= MIN_VOTES and n / total >= MIN_TEAM_SHARE:
            out[key] = (name, n / total, n)
    # two Olimp names may map to one football-data team (aliases) - fine; one name -> one team only
    return out


def write(tmap, team_map) -> tuple[int, int]:
    with session() as s:
        fd_ext = {(t, e): i for t, e, i in s.execute(
            select(SourceEntity.entity_type, SourceEntity.external_id, SourceEntity.entity_id)
            .where(SourceEntity.source_id == FD_SOURCE, SourceEntity.entity_type.in_(("league", "team"))))}
        s.execute(delete(SourceEntity).where(SourceEntity.source_id == SOURCE, SourceEntity.method == "vote",
                                             SourceEntity.entity_type.in_(("league", "team"))))
        manual = {(t, e) for t, e in s.execute(select(SourceEntity.entity_type, SourceEntity.external_id)
                                               .where(SourceEntity.source_id == SOURCE))}
        n_l = n_t = 0
        for tourn, (div, share) in tmap.items():
            lid = fd_ext.get(("league", div))
            if lid and ("league", tourn) not in manual:
                s.add(SourceEntity(source_id=SOURCE, entity_type="league", external_id=tourn, external_name=tourn,
                                   entity_id=lid, method="vote", confidence=Decimal(f"{share:.3f}")))
                n_l += 1
        for (country, ol_name), (fd_name, share, _) in team_map.items():
            tid = fd_ext.get(("team", f"{country}|{fd_name}"))
            key = f"{country}|{ol_name}"
            if tid and ("team", key) not in manual:
                s.add(SourceEntity(source_id=SOURCE, entity_type="team", external_id=key, external_name=ol_name,
                                   entity_id=tid, method="vote", confidence=Decimal(f"{min(share, 1):.3f}")))
                n_t += 1
    return n_l, n_t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sqlite", default=str(fd.DB))
    args = ap.parse_args()
    ol, fdf = olimp_frame(args.sqlite), fd_frame(args.sqlite)
    print(f"Olimp matches of football-data countries (senior men): {len(ol)}; football-data: {len(fdf)}", flush=True)
    cand = candidates(ol, fdf)
    tmap = map_tournaments(cand, ol)
    team_map = map_teams(cand, tmap)
    print(f"candidate pairs {len(cand)}; tournaments mapped {len(tmap)} of {ol['tournament'].nunique()}; "
          f"teams mapped {len(team_map)}")
    for t, (div, share) in sorted(tmap.items(), key=lambda x: (x[1][0], -x[1][1]))[:60]:
        print(f"  {div:<4} {share:5.0%}  {t}")
    sample = sorted(team_map.items())[:15]
    for (country, ol_name), (fd_name, share, n) in sample:
        print(f"  {country:<10} {ol_name:<24} -> {fd_name:<20} {share:4.0%} ({n})")
    if not args.dry_run:
        n_l, n_t = write(tmap, team_map)
        print(f"written: {n_l} league and {n_t} team mappings")


if __name__ == "__main__":
    main()
