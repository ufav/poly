"""Olimp results archive football (data/football.sqlite ol_matches, scripts/import_olimp_football.py) -> PostgreSQL.

- a match found in football-data (both teams mapped by data_engine.normalization.olimp_football, date +-1 day) is
  linked (source_entities) and gets what football-data lacks: goal events with minutes, corners, shots on target,
  offsides, possession (source olimp_archive);
- otherwise a new match: league = mapped league or a new one from the tournament ('Футбол. Англия. Кубок лиги' ->
  country England / name 'Кубок лиги', kind cup, women / youth from the name); team = mapped team, else one team per
  Olimp name (Olimp names are unique across tournaments: 'Спартак М' in the Russian league and in the Champions
  League is one team), named in Russian until an English source maps it;
- archive time is Almaty local time (UTC+6 until 2024-03, +5 after); cancelled / postponed rows keep their status.
Re-run skips matches already imported.

  python -m data_engine.legacy.olimp_archive
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db import session
from core.models import League, Match, MatchEvent, MatchStatistic, SourceEntity, Team
from data_engine.normalization.olimp_football import COUNTRY
from pmbot import footballdata as fd
from pmbot.olimp_archive import _STAT_SUFFIX, _STAT_WORDS, _SUFFIX

SPORT = 1
SOURCE = 2
FD_SOURCE = 1
ALMATY = ZoneInfo("Asia/Almaty")
CHUNK = 20000
NON_COUNTRY = {"Лига Чемпионов УЕФА", "Лига чемпионов УЕФА", "Лига Чемпионов UEFA", "Лига Европы УЕФА",
               "Лига Европы UEFA", "Лига конференций УЕФА", "Товарищеские матчи", "Сборные", "Чемпионат мира",
               "Чемпионат Европы", "Клубные товарищеские матчи", "Женщины", "Суперкубок УЕФА", "Лига наций УЕФА",
               "Кубок Либертадорес", "Южноамериканский кубок", "Лига чемпионов АФК", "Лига чемпионов КАФ",
               "Лига чемпионов КОНКАКАФ"}
_AGE = re.compile(r"до (\d\d) лет|U(\d\d)")


def utc(local: str) -> dt.datetime:
    d = dt.datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=ALMATY)
    return d.astimezone(dt.timezone.utc)


def parse_tournament(t: str) -> dict:
    """'Футбол. Англия. Кубок лиги. 1/8 финала' -> country, name, kind, gender, age_group, stage."""
    parts = t.split(". ")[1:]  # drop 'Футбол'
    gender = "f" if "Женщины" in parts else "m"
    parts = [p for p in parts if p != "Женщины"]
    country = None
    if parts and parts[0] not in NON_COUNTRY and not parts[0].startswith(("Лига", "Кубок", "Чемпионат", "Товарищ")):
        country = COUNTRY.get(parts[0], parts[0])
        parts = parts[1:]
    name = ". ".join(parts) or t
    m = _AGE.search(name)
    age = f"U{m.group(1) or m.group(2)}" if m else None
    low = name.lower()
    kind = "friendly" if "товарищ" in low else "cup" if "кубок" in low or "cup" in low else \
        "international" if country is None else "league"
    return {"country": country, "name": name[:160], "kind": kind, "gender": gender, "age_group": age}


def pseudo(r) -> bool:
    """Matchday totals / special bets / player battles stored as matches by older archive parses."""
    if any(w in r["tournament"] for w in _STAT_WORDS):
        return True
    sh, sa = _SUFFIX.match(r["home"]), _SUFFIX.match(r["away"])
    return bool(sh and sa and sh.group(2) == sa.group(2) and sh.group(2) in _STAT_SUFFIX)


def status_of(raw: str, has_score: bool) -> str | None:
    low = (raw or "").lower()
    if has_score:
        return "finished"
    if "отмен" in low:
        return "cancelled"
    if "перен" in low:
        return "postponed"
    if "прерв" in low:
        return "abandoned"
    return None


class Refs:
    def __init__(self, s) -> None:
        self.s = s
        self.fd_ext = {(t, e): i for t, e, i in s.execute(
            select(SourceEntity.entity_type, SourceEntity.external_id, SourceEntity.entity_id)
            .where(SourceEntity.source_id == FD_SOURCE, SourceEntity.entity_type.in_(("league", "team"))))}
        self.ext = {(t, e): i for t, e, i in s.execute(
            select(SourceEntity.entity_type, SourceEntity.external_id, SourceEntity.entity_id)
            .where(SourceEntity.source_id == SOURCE, SourceEntity.entity_type.in_(("league", "team"))))}
        # Olimp name -> mapped football-data team when the name is mapped in exactly one country
        by_name = defaultdict(set)
        for (etype, key), tid in self.ext.items():
            if etype == "team" and "|" in key:
                by_name[key.split("|", 1)[1]].add(tid)
        self.mapped_name = {n: next(iter(ids)) for n, ids in by_name.items() if len(ids) == 1}
        self.leagues = {(l.sport_id, l.country, l.name, l.gender, l.age_group): l.id
                        for l in s.scalars(select(League).where(League.sport_id == SPORT))}

    def league(self, tournament: str) -> int:
        if ("league", tournament) in self.ext:
            return self.ext[("league", tournament)]
        p = parse_tournament(tournament)
        key = (SPORT, p["country"], p["name"], p["gender"], p["age_group"])
        if key not in self.leagues:
            lg = League(sport_id=SPORT, country=p["country"], name=p["name"], kind=p["kind"], gender=p["gender"],
                        age_group=p["age_group"])
            self.s.add(lg)
            self.s.flush()
            self.leagues[key] = lg.id
        self.s.add(SourceEntity(source_id=SOURCE, entity_type="league", external_id=tournament,
                                external_name=tournament, entity_id=self.leagues[key], method="provider"))
        self.ext[("league", tournament)] = self.leagues[key]
        return self.leagues[key]

    def team(self, name: str, country: str | None, gender: str) -> int:
        if country and ("team", f"{country}|{name}") in self.ext:
            return self.ext[("team", f"{country}|{name}")]
        if name in self.mapped_name:
            return self.mapped_name[name]
        key = f"name|{name}"
        if ("team", key) not in self.ext:
            t = Team(sport_id=SPORT, name=name[:160], country=country, kind="club", gender=gender)
            self.s.add(t)
            self.s.flush()
            self.s.add(SourceEntity(source_id=SOURCE, entity_type="team", external_id=key, external_name=name,
                                    entity_id=t.id, method="provider"))
            self.ext[("team", key)] = t.id
        return self.ext[("team", key)]


def fd_index(s) -> dict[tuple[int, int, dt.date], int]:
    """(home team, away team, UTC date) -> football-data match id."""
    rows = s.execute(select(Match.id, Match.home_team_id, Match.away_team_id, Match.start_time)
                     .join(SourceEntity, (SourceEntity.entity_id == Match.id) & (SourceEntity.entity_type == "match")
                           & (SourceEntity.source_id == FD_SOURCE)))
    return {(h, a, st.date()): mid for mid, h, a, st in rows}


def extras(r: sqlite3.Row, match_id: int) -> tuple[list[dict], list[dict]]:
    events = [{"match_id": match_id, "source_id": SOURCE, "type": "goal", "minute": m, "side": side}
              for m, side in json.loads(r["goals"] or "[]")]
    stats = []
    for stat, h, a in (("corners", "hc", "ac"), ("shots_on_target", "hst", "ast"), ("offsides", "hoff", "aoff"),
                       ("possession", "hposs", "aposs")):
        if r[h] is not None and r[a] is not None:
            stats.append({"match_id": match_id, "source_id": SOURCE, "stat": stat, "period": 0,
                          "home_value": Decimal(r[h]), "away_value": Decimal(r[a])})
    return events, stats


def main() -> None:
    src = sqlite3.connect(f"file:{fd.DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    total = src.execute("SELECT COUNT(*) FROM ol_matches").fetchone()[0]
    with session() as s:
        done = set(s.scalars(select(SourceEntity.external_id).where(SourceEntity.source_id == SOURCE,
                                                                    SourceEntity.entity_type == "match")))
        fd_matches = fd_index(s)
    print(f"Olimp archive football -> PostgreSQL: {total} rows, {len(done)} already imported, "
          f"{len(fd_matches)} football-data matches to link against", flush=True)
    t0, seen, stats_count, extras_done = time.time(), 0, Counter(), set()
    cur = src.execute("SELECT * FROM ol_matches ORDER BY dt")
    while True:
        rows = cur.fetchmany(CHUNK)
        if not rows:
            break
        seen += len(rows)
        with session() as s:
            refs = Refs(s)
            new_matches, new_keys, links, events, stats = [], [], [], [], []
            for r in rows:
                key = f"{r['tournament']}|{r['dt']}|{r['home']}|{r['away']}"
                if key in done or pseudo(r):
                    continue
                has_score = r["fthg"] is not None and r["ftag"] is not None
                status = status_of(r["raw"], has_score)
                if status is None:
                    stats_count["no score"] += 1
                    continue
                start = utc(r["dt"])
                league_id = refs.league(r["tournament"])
                p = parse_tournament(r["tournament"])
                home = refs.team(r["home"], p["country"], p["gender"])
                away = refs.team(r["away"], p["country"], p["gender"])
                fd_id = next((fd_matches[(home, away, start.date() + dt.timedelta(days=d))] for d in (0, -1, 1)
                              if (home, away, start.date() + dt.timedelta(days=d)) in fd_matches), None)
                done.add(key)
                if fd_id:
                    links.append((key, r, fd_id))
                    continue
                periods = [[r["hthg"], r["htag"]], [r["fthg"] - r["hthg"], r["ftag"] - r["htag"]]] \
                    if has_score and r["hthg"] is not None else None
                extra = {k: r[k] for k in ("added1", "added2", "yellow", "red", "penalty") if r[k] is not None}
                new_matches.append({"sport_id": SPORT, "league_id": league_id, "start_time": start,
                                    "home_team_id": home, "away_team_id": away, "status": status,
                                    "home_score": r["fthg"], "away_score": r["ftag"], "periods": periods,
                                    "result_extra": extra or None})
                new_keys.append((key, r))
            ids = s.scalars(insert(Match).returning(Match.id, sort_by_parameter_order=True), new_matches).all() \
                if new_matches else []
            mapping = [(key, r, mid, "provider") for (key, r), mid in zip(new_keys, ids)] + \
                      [(key, r, mid, "linked") for key, r, mid in links]
            if mapping:
                s.execute(insert(SourceEntity), [
                    {"source_id": SOURCE, "entity_type": "match", "external_id": key[:255],
                     "external_name": f"{r['home']} - {r['away']}"[:255], "entity_id": mid, "method": method}
                    for key, r, mid, method in mapping])
            for _, r, mid, _ in mapping:
                if mid in extras_done:  # the same match under two tournament names
                    continue
                extras_done.add(mid)
                ev, st = extras(r, mid)
                events += ev
                stats += st
            if events:
                s.execute(insert(MatchEvent), events)
            if stats:
                s.execute(pg_insert(MatchStatistic).on_conflict_do_nothing(), stats)
            stats_count["new"] += len(ids)
            stats_count["linked"] += len(links)
        eta = (time.time() - t0) / seen * (total - seen)
        print(f"[{seen:8d}/{total}] {rows[-1]['dt'][:7]}  new {stats_count['new']:8d}  linked to football-data "
              f"{stats_count['linked']:7d}  no score {stats_count['no score']:6d}   "
              f"ETA {int(eta // 60)}m{int(eta % 60):02d}s", flush=True)
    print(f"done in {(time.time() - t0) / 60:.1f} min: {dict(stats_count)}")


if __name__ == "__main__":
    main()
