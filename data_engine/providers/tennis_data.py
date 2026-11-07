"""tennis-data.co.uk (ATP / WTA main tour: 250 and up, Grand Slams; updated weekly) -> PostgreSQL: matches with
the set score, games by set, surface / court / round / best of, retirements and walkovers; entry rankings and
points of both players (match_statistics 'rank', 'rank_points'); closing odds (the last before play: Bet365,
Pinnacle, Oddsportal max / average, Betfair Exchange) as the moneyline market.

The files are found through the site's download page (alldata.php links). Players: 'De Minaur A.' / 'Fernandez
L.A.' -> our player of the same gender whose surname matches and whose first name(s) start with the initials (the
one with clearly most matches among several), else a new player. A match already in the database (Olimpbet,
Polymarket: the same two players within -1 .. +2 days of the date) is linked - it gets the odds and ranks, and the
result if it has none -, else a new match is created with home / away in name order (the file lists winner /
loser). Rows already imported are skipped.

  python -u -m data_engine.providers.tennis_data [--years 2024,2025,2026]
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import re
import time
import unicodedata
from collections import defaultdict

import pandas as pd
from sqlalchemy import insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db import session
from core.models import League, Market, Match, MatchStatistic, OddsQuote, SourceEntity, Team
from pmbot.http import HttpClient

SOURCE = 5   # tennis_data
SPORT = 2
BASE = "https://www.tennis-data.co.uk"
BOOKS = {"B365": "bet365", "PS": "pinnacle", "Max": "market_max", "Avg": "market_avg", "BFE": "betfair_ex"}
MAIN_TOUR = re.compile(r"^(ATP|WTA|Australian Open|Roland Garros|Wimbledon|US Open|United Cup|Davis Cup|Billie)")


def tokens(name: str) -> list[str]:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return [w for w in re.split(r"[^a-z]+", s) if w]


def split_td(name: str) -> tuple[list[str], list[str]]:
    """'Fernandez L.A.' -> (['fernandez'], ['l', 'a']); 'De Minaur A.' -> (['de', 'minaur'], ['a'])."""
    parts = name.strip().split(" ")
    initials = []
    while len(parts) > 1 and re.fullmatch(r"([A-Za-z]\.)+|[A-Za-z]", parts[-1]):
        initials = re.findall(r"[A-Za-z]", parts.pop()) + initials
    return tokens(" ".join(parts)), [i.lower() for i in initials]


class Players:
    """tennis-data player names -> our players."""

    def __init__(self, s) -> None:
        self.s = s
        self.own = {e: i for e, i in s.execute(select(SourceEntity.external_id, SourceEntity.entity_id).where(
            SourceEntity.source_id == SOURCE, SourceEntity.entity_type == "team"))}
        n_matches = dict(s.execute(text("""
            SELECT tid, count(*) FROM (SELECT home_team_id tid FROM matches WHERE sport_id = :sp
                                       UNION ALL SELECT away_team_id FROM matches WHERE sport_id = :sp) x
            GROUP BY tid"""), {"sp": SPORT}).all())
        self.index: dict[tuple[str, str], list[tuple[int, list[str], int]]] = defaultdict(list)
        for tid, name, gender in s.execute(text("SELECT id, name, gender FROM teams WHERE sport_id = :sp "
                                                "AND kind = 'player'"), {"sp": SPORT}):
            t = tokens(name)
            for k in range(1, len(t)):  # 'Surname First ...' and 'First ... Surname'
                self.index[(gender, " ".join(t[:k]))].append((tid, t[k:], n_matches.get(tid, 0)))
                self.index[(gender, " ".join(t[k:]))].append((tid, t[:k], n_matches.get(tid, 0)))
        self.created = self.matched = 0

    def find(self, name: str, gender: str) -> int | None:
        surname, initials = split_td(name)
        if not surname:
            return None
        cands = {}
        for tid, first, n in self.index.get((gender, " ".join(surname)), []):
            if not initials or (len(first) >= 1 and first[0].startswith(initials[0])
                                and all(any(f.startswith(i) for f in first) for i in initials)):
                cands[tid] = n
        if not cands:
            return None
        ranked = sorted(cands.items(), key=lambda x: -x[1])
        if len(ranked) == 1 or ranked[0][1] >= 3 * max(ranked[1][1], 1):
            return ranked[0][0]
        return None

    def get(self, tour: str, name: str, gender: str) -> int:
        key = f"{tour}|{name}"
        if key in self.own:
            return self.own[key]
        tid = self.find(name, gender)
        method = "name" if tid else "provider"
        if tid:
            self.matched += 1
        else:
            team = Team(sport_id=SPORT, name=name[:160], kind="player", gender=gender)
            self.s.add(team)
            self.s.flush()
            tid = team.id
            surname, _ = split_td(name)
            self.index[(gender, " ".join(surname))].append((tid, [i.lower() for i in split_td(name)[1]], 0))
            self.created += 1
        self.s.add(SourceEntity(source_id=SOURCE, entity_type="team", external_id=key, external_name=name,
                                entity_id=tid, method=method))
        self.own[key] = tid
        return tid


def file_links(http: HttpClient, years: list[int]) -> list[tuple[str, int, str]]:
    """(tour, year, url) from the download page."""
    page = http.get(f"{BASE}/alldata.php").text
    out = []
    for href in re.findall(r'href="([^"]+/(\d{4})(w?)/\d{4}\.xlsx?)"', page, re.IGNORECASE):
        path, year, w = href
        if int(year) in years:
            out.append(("WTA" if w else "ATP", int(year), f"{BASE}/{path.lstrip('/')}"))
    return out


def num(v):
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def league_id(s, cache: dict, tour: str, tournament: str, gender: str) -> int:
    key = f"{tour}|{tournament}"
    if key not in cache:
        lid = s.scalar(select(SourceEntity.entity_id).where(SourceEntity.source_id == SOURCE,
                                                            SourceEntity.entity_type == "league",
                                                            SourceEntity.external_id == key))
        if not lid:
            lg = League(sport_id=SPORT, country=None, name=f"{tour}. {tournament}"[:160], gender=gender,
                        kind="tournament")
            s.add(lg)
            s.flush()
            lid = lg.id
            s.add(SourceEntity(source_id=SOURCE, entity_type="league", external_id=key, external_name=tournament,
                               entity_id=lid, method="provider"))
        cache[key] = lid
    return cache[key]


def import_file(tour: str, year: int, df: pd.DataFrame) -> dict:
    gender = "f" if tour == "WTA" else "m"
    tier = "Tier" if "Tier" in df else "Series"
    cnt = defaultdict(int)
    with session() as s:
        done = set(s.scalars(select(SourceEntity.external_id).where(
            SourceEntity.source_id == SOURCE, SourceEntity.entity_type == "match",
            SourceEntity.external_id.like(f"{tour}|{year}|%"))))
        players = Players(s)
        leagues: dict = {}
        lo, hi = df["Date"].min() - pd.Timedelta(days=3), df["Date"].max() + pd.Timedelta(days=3)
        existing = defaultdict(list)
        for mid, h, a, st, status in s.execute(text("""
                SELECT m.id, m.home_team_id, m.away_team_id, m.start_time, m.status FROM matches m
                WHERE m.sport_id = :sp AND m.start_time BETWEEN :lo AND :hi AND NOT EXISTS (
                  SELECT 1 FROM source_entities se WHERE se.source_id = :src AND se.entity_type = 'match'
                    AND se.entity_id = m.id)"""), {"sp": SPORT, "lo": lo.to_pydatetime(), "hi": hi.to_pydatetime(),
                                                   "src": SOURCE}):
            existing[frozenset((h, a))].append((mid, h, st.astimezone(dt.timezone.utc).date(), status))
        for i, r in df.iterrows():
            key = f"{tour}|{year}|{r[tour]}|{r['Round']}|{r['Winner']}|{r['Loser']}"
            if key in done or not isinstance(r["Winner"], str) or not isinstance(r["Loser"], str):
                continue
            day = r["Date"].date()
            w, lo_ = players.get(tour, r["Winner"], gender), players.get(tour, r["Loser"], gender)
            sets_w, sets_l = num(r.get("Wsets")), num(r.get("Lsets"))
            games = [(num(r.get(f"W{k}")), num(r.get(f"L{k}"))) for k in range(1, 6)]
            games = [(int(a), int(b)) for a, b in games if a is not None and b is not None]
            comment = str(r.get("Comment") or "Completed")
            extra = {"surface": r.get("Surface"), "court": r.get("Court"), "series": r.get(tier),
                     "best_of": int(num(r.get("Best of")) or 3)}
            if comment != "Completed":
                extra["comment"] = comment
            hit = next(((mid, h, status) for mid, h, d0, status in existing.get(frozenset((w, lo_)), [])
                        if -1 <= (d0 - day).days <= 2), None)
            if hit:
                mid, home, status = hit
                cnt["linked"] += 1
            else:
                home = w if tokens(r["Winner"]) <= tokens(r["Loser"]) else lo_
                status = None
            win_home = home == w
            hs, as_ = (sets_w, sets_l) if win_home else (sets_l, sets_w)
            periods = [[a, b] if win_home else [b, a] for a, b in games] or None
            st = "walkover" if comment == "Walkover" else "finished"
            if not hit:
                mid = s.scalar(insert(Match).values(
                    sport_id=SPORT, league_id=league_id(s, leagues, tour, str(r["Tournament"]), gender),
                    stage=str(r["Round"])[:64], start_time=dt.datetime.combine(day, dt.time(), dt.timezone.utc),
                    time_known=False, home_team_id=home, away_team_id=lo_ if home == w else w, status=st,
                    home_score=None if st == "walkover" or hs is None else int(hs),
                    away_score=None if st == "walkover" or as_ is None else int(as_),
                    periods=periods, result_extra=extra).returning(Match.id))
                cnt["new"] += 1
            else:
                s.execute(text("UPDATE matches SET result_extra = COALESCE(result_extra, '{}'::jsonb) || "
                               "CAST(:x AS jsonb) WHERE id = :id"), {"x": json.dumps(extra, default=str), "id": mid})
                if status != "finished" and st == "finished" and hs is not None:
                    s.execute(text("UPDATE matches SET status = 'finished', home_score = :h, away_score = :a, "
                                   "periods = CAST(:p AS jsonb) WHERE id = :id"),
                              {"h": int(hs), "a": int(as_), "p": json.dumps(periods) if periods else None, "id": mid})
                    cnt["filled"] += 1
            s.add(SourceEntity(source_id=SOURCE, entity_type="match", external_id=key,
                               external_name=f"{r['Winner']} - {r['Loser']}", entity_id=mid,
                               method="linked" if hit else "provider"))
            stats = []
            for stat, wc, lc in (("rank", "WRank", "LRank"), ("rank_points", "WPts", "LPts")):
                wv, lv = num(r.get(wc)), num(r.get(lc))
                if wv is not None and lv is not None:
                    stats.append({"match_id": mid, "source_id": SOURCE, "stat": stat, "period": 0,
                                  "home_value": wv if win_home else lv, "away_value": lv if win_home else wv})
            if stats:
                s.execute(pg_insert(MatchStatistic).on_conflict_do_nothing(), stats)
            quotes = []
            for col, book in BOOKS.items():
                ow, ol = num(r.get(f"{col}W")), num(r.get(f"{col}L"))
                if ow and ol and ow > 1 and ol > 1:
                    quotes += [("home" if win_home else "away", book, ow), ("away" if win_home else "home", book, ol)]
            if quotes:
                market = s.scalar(pg_insert(Market).values(match_id=mid, kind="moneyline", period=0, line=None)
                                  .on_conflict_do_update(constraint="markets_match_id_kind_period_line_key",
                                                         set_={"kind": "moneyline"}).returning(Market.id))
                s.execute(insert(OddsQuote), [{"market_id": market, "outcome": o, "source_id": SOURCE,
                                               "bookmaker": b, "snapshot": "closing", "odds": v}
                                              for o, b, v in quotes])
                cnt["odds"] += len(quotes)
        cnt["players_matched"], cnt["players_new"] = players.matched, players.created
    return dict(cnt)


def main() -> None:
    ap = argparse.ArgumentParser()
    this = dt.date.today().year
    ap.add_argument("--years", default=f"{this - 2},{this - 1},{this}")
    args = ap.parse_args()
    years = [int(y) for y in args.years.split(",")]
    http = HttpClient(min_interval=2.0, retries=4, timeout=120)
    t0 = time.time()
    links = file_links(http, years)
    # newest first: the players of the recent years are linked through Olimpbet matches, older rows reuse them
    for tour, year, url in sorted(links, key=lambda x: (-x[1], x[0])):
        r = http.get(url)
        r.raise_for_status()
        df = pd.read_excel(io.BytesIO(r.content))
        df = df[df["Date"].notna()].copy()
        df["Date"] = pd.to_datetime(df["Date"])
        print(f"{tour} {year}: {len(df)} rows -> {import_file(tour, year, df)}", flush=True)
    print(f"done in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
