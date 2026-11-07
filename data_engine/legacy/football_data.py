"""football-data.co.uk (data/football.sqlite fd_matches, scripts/collect_football.py) -> PostgreSQL: leagues, seasons,
teams, matches (score, halves), match_statistics, markets 1x2 / total 2.5 / Asian handicap with pre-match and closing
odds of Pinnacle, market average / max, Bet365, Betfair Exchange. Matches already imported are skipped (re-run adds
new ones); --season Y re-imports one season (current season updates).

Odds columns changed over time: individual bookmakers only before 2005 (market_avg = mean of them), Bb* aggregates
2005-2019, Avg* / Max* and closing *C* from 2019. Pre-match odds = taken ~1-2 days before kick-off (Fri / Tue),
snapshot 'pre'; closing = 'closing'. Asian handicap closing has its own line (AHCh). Times are UK time.

  python -m data_engine.legacy.football_data [--season 2025]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import time
from collections import defaultdict
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, insert, select

from core.db import session
from core.models import League, Market, Match, MatchStatistic, OddsQuote, Season, SourceEntity, Team
from pmbot import footballdata as fd

SPORT = 1
SOURCE = 1
UK = ZoneInfo("Europe/London")
LEVEL = {"E0": 1, "E1": 2, "E2": 3, "E3": 4, "EC": 5, "SC0": 1, "SC1": 2, "SC2": 3, "SC3": 4, "D1": 1, "D2": 2,
         "I1": 1, "I2": 2, "SP1": 1, "SP2": 2, "F1": 1, "F2": 2, "N1": 1, "B1": 1, "P1": 1, "T1": 1, "G1": 1}
EXTRA_COUNTRY = {"ARG": "Argentina", "AUT": "Austria", "BRA": "Brazil", "CHN": "China", "DNK": "Denmark",
                 "FIN": "Finland", "IRL": "Ireland", "JPN": "Japan", "MEX": "Mexico", "NOR": "Norway", "POL": "Poland",
                 "ROU": "Romania", "RUS": "Russia", "SWE": "Sweden", "SWZ": "Switzerland", "USA": "USA"}
AUTUMN_SPRING_EXTRA = {"AUT", "DNK", "POL", "ROU", "RUS", "SWZ"}
# copies of the La Liga file at the source (same fixtures and scores)
BROKEN_FILES = {("SP2", 1993), ("SP2", 1994), ("SP2", 1995), ("P1", 1993), ("SC1", 1993)}
OLD_BOOKS = ("B365", "BW", "IW", "LB", "PS", "WH", "VC", "GB", "SB", "SJ", "BS", "SO", "SY")

# (bookmaker, snapshot, {outcome: [file columns, first present wins]})
X12 = [
    ("pinnacle", "pre", {"home": ["PSH"], "draw": ["PSD"], "away": ["PSA"]}),
    ("pinnacle", "closing", {"home": ["PSCH"], "draw": ["PSCD"], "away": ["PSCA"]}),
    ("market_avg", "pre", {"home": ["AvgH", "BbAvH"], "draw": ["AvgD", "BbAvD"], "away": ["AvgA", "BbAvA"]}),
    ("market_avg", "closing", {"home": ["AvgCH"], "draw": ["AvgCD"], "away": ["AvgCA"]}),
    ("market_max", "pre", {"home": ["MaxH", "BbMxH"], "draw": ["MaxD", "BbMxD"], "away": ["MaxA", "BbMxA"]}),
    ("market_max", "closing", {"home": ["MaxCH"], "draw": ["MaxCD"], "away": ["MaxCA"]}),
    ("bet365", "pre", {"home": ["B365H"], "draw": ["B365D"], "away": ["B365A"]}),
    ("bet365", "closing", {"home": ["B365CH"], "draw": ["B365CD"], "away": ["B365CA"]}),
    ("betfair_ex", "pre", {"home": ["BFEH"], "draw": ["BFED"], "away": ["BFEA"]}),
    ("betfair_ex", "closing", {"home": ["BFECH"], "draw": ["BFECD"], "away": ["BFECA"]}),
]
OU25 = [
    ("pinnacle", "pre", {"over": ["P>2.5"], "under": ["P<2.5"]}),
    ("pinnacle", "closing", {"over": ["PC>2.5"], "under": ["PC<2.5"]}),
    ("market_avg", "pre", {"over": ["Avg>2.5", "BbAv>2.5"], "under": ["Avg<2.5", "BbAv<2.5"]}),
    ("market_avg", "closing", {"over": ["AvgC>2.5"], "under": ["AvgC<2.5"]}),
    ("market_max", "pre", {"over": ["Max>2.5", "BbMx>2.5"], "under": ["Max<2.5", "BbMx<2.5"]}),
    ("market_max", "closing", {"over": ["MaxC>2.5"], "under": ["MaxC<2.5"]}),
    ("bet365", "pre", {"over": ["B365>2.5"], "under": ["B365<2.5"]}),
    ("bet365", "closing", {"over": ["B365C>2.5"], "under": ["B365C<2.5"]}),
    ("betfair_ex", "pre", {"over": ["BFE>2.5"], "under": ["BFE<2.5"]}),
    ("betfair_ex", "closing", {"over": ["BFEC>2.5"], "under": ["BFEC<2.5"]}),
]
# (bookmaker, snapshot, line columns, {outcome: columns}); line = home handicap
AH = [
    ("pinnacle", "pre", ["AHh", "BbAHh"], {"home": ["PAHH"], "away": ["PAHA"]}),
    ("pinnacle", "closing", ["AHCh"], {"home": ["PCAHH"], "away": ["PCAHA"]}),
    ("market_avg", "pre", ["AHh", "BbAHh"], {"home": ["AvgAHH", "BbAvAHH"], "away": ["AvgAHA", "BbAvAHA"]}),
    ("market_avg", "closing", ["AHCh"], {"home": ["AvgCAHH"], "away": ["AvgCAHA"]}),
    ("market_max", "pre", ["AHh", "BbAHh"], {"home": ["MaxAHH", "BbMxAHH"], "away": ["MaxAHA", "BbMxAHA"]}),
    ("market_max", "closing", ["AHCh"], {"home": ["MaxCAHH"], "away": ["MaxCAHA"]}),
    ("bet365", "pre", ["AHh", "BbAHh"], {"home": ["B365AHH"], "away": ["B365AHA"]}),
    ("bet365", "closing", ["AHCh"], {"home": ["B365CAHH"], "away": ["B365CAHA"]}),
]
STATS = [("shots", "hs", "as_"), ("shots_on_target", "hst", "ast"), ("fouls", "hf", "af"), ("corners", "hc", "ac"),
         ("yellow", "hy", "ay"), ("red", "hr", "ar"), ("xg", "hxg", "axg")]


def _first(odds: dict, cols: list[str]) -> float | None:
    return next((odds[c] for c in cols if odds.get(c) and odds[c] > 1), None)


def quotes_1x2(odds: dict) -> list[tuple[str, str, str, float]]:
    """(bookmaker, snapshot, outcome, decimal odds); market_avg pre from individual books before the aggregates."""
    out = []
    for book, snap, cols in X12:
        got = {o: _first(odds, c) for o, c in cols.items()}
        if all(got.values()):
            out += [(book, snap, o, v) for o, v in got.items()]
    if not any(b == "market_avg" and s == "pre" for b, s, _, _ in out):
        books = [b for b in OLD_BOOKS if all((odds.get(f"{b}{x}") or 0) > 1 for x in "HDA")]
        if len(books) >= 2:
            out += [("market_avg", "pre", o, round(sum(odds[f"{b}{x}"] for b in books) / len(books), 3))
                    for o, x in (("home", "H"), ("draw", "D"), ("away", "A"))]
    return out


def quotes_two_way(odds: dict, spec) -> list[tuple[str, str, str, float]]:
    out = []
    for book, snap, cols in spec:
        got = {o: _first(odds, c) for o, c in cols.items()}
        if all(got.values()):
            out += [(book, snap, o, v) for o, v in got.items()]
    return out


def quotes_ah(odds: dict) -> dict[float, list[tuple[str, str, str, float]]]:
    """line -> quotes (the closing line may differ from the pre-match one)."""
    out = defaultdict(list)
    for book, snap, line_cols, cols in AH:
        line = next((odds[c] for c in line_cols if c in odds), None)
        got = {o: _first(odds, c) for o, c in cols.items()}
        if line is not None and all(got.values()):
            out[float(line)] += [(book, snap, o, v) for o, v in got.items()]
    return out


def season_name(div: str, season: int) -> str:
    if div in fd.MAIN_DIVS or div in AUTUMN_SPRING_EXTRA:
        return f"{season}/{(season + 1) % 100:02d}"
    return str(season)


def kickoff(date: str, time_: str | None) -> tuple[dt.datetime, bool]:
    d = dt.date.fromisoformat(date)
    if time_ and ":" in time_:
        h, m = (int(x) for x in time_.split(":")[:2])
        return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=UK).astimezone(dt.timezone.utc), True
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc), False


class Refs:
    """Get-or-create cache of leagues / seasons / teams with their source_entities."""

    def __init__(self, s) -> None:
        self.s = s
        self.ext = {(t, e): i for t, e, i in s.execute(
            select(SourceEntity.entity_type, SourceEntity.external_id, SourceEntity.entity_id)
            .where(SourceEntity.source_id == SOURCE, SourceEntity.entity_type.in_(("league", "team"))))}
        self.seasons = {(lg, n): i for i, lg, n in s.execute(select(Season.id, Season.league_id, Season.name))}

    def _map(self, etype: str, key: str, name: str, entity_id: int) -> None:
        self.s.add(SourceEntity(source_id=SOURCE, entity_type=etype, external_id=key, external_name=name,
                                entity_id=entity_id, method="provider"))
        self.ext[(etype, key)] = entity_id

    def league(self, div: str, league_name: str | None) -> tuple[int, str]:
        country, name = fd.MAIN_DIVS.get(div) or (EXTRA_COUNTRY[div], league_name or div)
        if ("league", div) not in self.ext:
            lg = League(sport_id=SPORT, country=country, name=name, level=LEVEL.get(div, 1), kind="league")
            self.s.add(lg)
            self.s.flush()
            self._map("league", div, f"{country}. {name}", lg.id)
        return self.ext[("league", div)], country

    def season(self, league_id: int, name: str) -> int:
        if (league_id, name) not in self.seasons:
            se = Season(league_id=league_id, name=name)
            self.s.add(se)
            self.s.flush()
            self.seasons[(league_id, name)] = se.id
        return self.seasons[(league_id, name)]

    def team(self, country: str, name: str) -> int:
        key = f"{country}|{name}"
        if ("team", key) not in self.ext:
            t = Team(sport_id=SPORT, name=name, country=country, kind="club")
            self.s.add(t)
            self.s.flush()
            self._map("team", key, name, t.id)
        return self.ext[("team", key)]


def import_group(s, refs: Refs, rows: list[sqlite3.Row], existing: set[str]) -> tuple[int, int]:
    """One league season; returns (matches, odds rows) inserted."""
    div, season = rows[0]["div"], rows[0]["season"]
    league_id, country = refs.league(div, rows[0]["league"])
    season_id = refs.season(league_id, season_name(div, season))
    new = [r for r in rows if f"{r['div']}|{r['date']}|{r['home']}|{r['away']}" not in existing]
    if not new:
        return 0, 0
    values = []
    for r in new:
        start, known = kickoff(r["date"], r["time"])
        periods = None
        if r["hthg"] is not None and r["htag"] is not None:
            periods = [[r["hthg"], r["htag"]], [r["fthg"] - r["hthg"], r["ftag"] - r["htag"]]]
        values.append({"sport_id": SPORT, "league_id": league_id, "season_id": season_id, "start_time": start,
                       "time_known": known, "home_team_id": refs.team(country, r["home"]),
                       "away_team_id": refs.team(country, r["away"]), "status": "finished",
                       "home_score": r["fthg"], "away_score": r["ftag"], "periods": periods})
    ids = s.scalars(insert(Match).returning(Match.id, sort_by_parameter_order=True), values).all()
    s.execute(insert(SourceEntity), [
        {"source_id": SOURCE, "entity_type": "match", "external_id": f"{r['div']}|{r['date']}|{r['home']}|{r['away']}",
         "external_name": f"{r['home']} - {r['away']}", "entity_id": mid, "method": "provider"}
        for r, mid in zip(new, ids)])

    stats = []
    for r, mid in zip(new, ids):
        for stat, hc, ac in STATS:
            if r[hc] is not None and r[ac] is not None:
                stats.append({"match_id": mid, "source_id": SOURCE, "stat": stat, "period": 0,
                              "home_value": Decimal(str(r[hc])), "away_value": Decimal(str(r[ac]))})
    if stats:
        s.execute(insert(MatchStatistic), stats)

    market_rows, market_quotes = [], []
    for r, mid in zip(new, ids):
        odds = json.loads(r["odds"] or "{}")
        q = quotes_1x2(odds)
        if q:
            market_rows.append({"match_id": mid, "kind": "1x2", "period": 0, "line": None})
            market_quotes.append(q)
        q = quotes_two_way(odds, OU25)
        if q:
            market_rows.append({"match_id": mid, "kind": "total", "period": 0, "line": Decimal("2.5")})
            market_quotes.append(q)
        for line, q in quotes_ah(odds).items():
            market_rows.append({"match_id": mid, "kind": "spread", "period": 0, "line": Decimal(str(line))})
            market_quotes.append(q)
    n_odds = 0
    if market_rows:
        mids = s.scalars(insert(Market).returning(Market.id, sort_by_parameter_order=True), market_rows).all()
        quotes = [{"market_id": market_id, "outcome": o, "source_id": SOURCE, "bookmaker": b, "snapshot": snap,
                   "odds": Decimal(str(v))}
                  for market_id, q in zip(mids, market_quotes) for b, snap, o, v in q]
        s.execute(insert(OddsQuote), quotes)
        n_odds = len(quotes)
    return len(new), n_odds


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, help="re-import this season start year (drops its matches first)")
    ap.add_argument("--sqlite", default=str(fd.DB))
    args = ap.parse_args()

    src = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    groups = src.execute("SELECT div, season, COUNT(*) FROM fd_matches GROUP BY div, season ORDER BY season, div") \
        .fetchall()
    groups = [g for g in groups if (g[0], g[1]) not in BROKEN_FILES]
    if args.season:
        groups = [g for g in groups if g[1] == args.season]
    with session() as s:
        if args.season:
            keys = {k for (k,) in src.execute("SELECT div || '|' || date || '|' || home || '|' || away FROM fd_matches "
                                               "WHERE season = ?", (args.season,))}
            old = s.execute(select(SourceEntity.id, SourceEntity.external_id, SourceEntity.entity_id).where(
                SourceEntity.source_id == SOURCE, SourceEntity.entity_type == "match")).all()
            drop = [(i, e) for i, k, e in old if k in keys]
            if drop:
                s.execute(delete(Match).where(Match.id.in_([e for _, e in drop])))
                s.execute(delete(SourceEntity).where(SourceEntity.id.in_([i for i, _ in drop])))
        existing = set(s.scalars(select(SourceEntity.external_id).where(
            SourceEntity.source_id == SOURCE, SourceEntity.entity_type == "match")))
    total = sum(g[2] for g in groups)
    print(f"football-data -> PostgreSQL: {len(groups)} league seasons, {total} rows ({len(existing)} matches "
          f"already imported)", flush=True)
    t0, done, n_matches, n_odds = time.time(), 0, 0, 0
    for i, (div, season, n) in enumerate(groups, 1):
        rows = src.execute("SELECT * FROM fd_matches WHERE div = ? AND season = ? ORDER BY date", (div, season)) \
            .fetchall()
        with session() as s:
            refs = Refs(s)
            m, o = import_group(s, refs, rows, existing)
        n_matches, n_odds, done = n_matches + m, n_odds + o, done + n
        eta = (time.time() - t0) / done * (total - done)
        print(f"[{i:4d}/{len(groups)}] {div:<4} {season}  +{m:4d} matches +{o:6d} odds   "
              f"total {n_matches:7d} / {n_odds:8d}   ETA {int(eta // 60)}m{int(eta % 60):02d}s", flush=True)
    print(f"done in {(time.time() - t0) / 60:.1f} min: {n_matches} matches, {n_odds} odds rows")


if __name__ == "__main__":
    main()
