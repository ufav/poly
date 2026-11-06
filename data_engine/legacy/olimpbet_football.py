"""Olimpbet results API football (data/football.sqlite ob_fb_events from scripts/collect_olimpbet_football.py; only the
last ~14 months have scores) -> PostgreSQL.

- team: Olimpbet competitor id (stable) -> the Olimp archive team with the same Russian name (ob_fb_names_ru from
  scripts/collect_olimpbet_ru_names.py; the archive and the API are the same site), else a new team; an archive team
  still named in Russian gets the English API name;
- league: tournament id -> the archive league of the same Russian tournament name (stage cut off), else a new league
  named in English; the stage goes to matches.stage;
- match: a finished match with a score; one already in the database (football-data, same teams, +-1 day) is linked;
  statistics events (corners, yellow cards, shots, shots on target, fouls, offsides ... whole match and by half)
  go to match_statistics (source olimpbet). Matchday totals / player battles / special bets are skipped.
Re-run skips events already imported.

  python -m data_engine.legacy.olimpbet_football
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
from collections import Counter

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db import session
from core.models import League, Match, MatchStatistic, SourceEntity, Team
from data_engine.legacy.olimp_archive import parse_tournament
from data_engine.normalization.olimp_cleanup import norm, split_stage
from pmbot import footballdata as fd

SPORT = 1
SOURCE = 3          # olimpbet
OLIMP = 2
CHUNK = 20000
PSEUDO = re.compile(r"Players Battle|statistics|Statistics|Special bets|Specials|Head-to-head", re.IGNORECASE)
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
EN_STAGE = re.compile(
    r"^(\d+(st|nd|rd|th) |1/\d+ final|final|semi-?final|quarter-?final|group stage|group\b|groups\b|league phase|"
    r"round\b|round of|qualification|qualifying|preliminary|play-?offs?|play-?out|knockout|first leg|second leg|"
    r"matchday|tour \d|apertura|clausura|championship group|relegation group|regular season|stage\b|"
    r"conference|zone|north|south|east|west)", re.IGNORECASE)
STATS = {"Corners": "corners", "Yellow cards": "yellow", "Shots": "shots", "Shots on target": "shots_on_target",
         "Fouls": "fouls", "Offsides": "offsides", "Throw-ins": "throw_ins", "Goal kicks": "goal_kicks",
         "Post and crossbar": "woodwork", "Saves": "saves", "Substitutions": "substitutions",
         "% completed passes": "pass_pct", "Successful tackles": "tackles", "Interceptions": "interceptions",
         "Successful dribbles": "dribbles", "Aerial duels won": "aerials_won", "Video reviews": "var_reviews",
         "Medical team on the field": "medical"}


def split_en(name: str) -> tuple[str, str | None]:
    parts = name.split(". ")
    for i, p in enumerate(parts):
        if i > 0 and EN_STAGE.match(p.strip()):
            return ". ".join(parts[:i]), ". ".join(parts[i:])
    return name, None


def en_tournament(name: str) -> dict:
    """'England. FA Cup. 3rd round' / 'UEFA Champions League. League phase' -> country, base, stage, gender, age."""
    base, stage = split_en(name)
    parts = base.split(". ")
    gender = "f" if re.search(r"\bWomen\b", base) else "m"
    age = (re.search(r"\bU(\d\d)\b", base) or [None, None])[1]
    international = re.match(r"(UEFA|FIFA|CAF|AFC|CONCACAF|CONMEBOL|Copa Libertadores|Copa Sudamericana|World Cup|"
                             r"Friendly|Club Friendly|International|Olympic|European Championship)", parts[0])
    country = None if international or len(parts) == 1 else parts[0]
    league = base if country is None else ". ".join(parts[1:])
    return {"country": country, "name": league[:160], "stage": stage, "gender": gender,
            "age_group": f"U{age}" if age else None}


class Refs:
    def __init__(self, s, names_ru: dict[tuple[str, int], str]) -> None:
        self.s = s
        self.names_ru = names_ru
        ext = s.execute(select(SourceEntity.source_id, SourceEntity.entity_type, SourceEntity.external_id,
                               SourceEntity.entity_id)
                        .where(SourceEntity.source_id.in_((OLIMP, SOURCE)),
                               SourceEntity.entity_type.in_(("league", "team")))).all()
        self.own = {(t, e): i for src, t, e, i in ext if src == SOURCE}
        self.archive_league = {e: i for src, t, e, i in ext if src == OLIMP and t == "league"}
        by_name: dict[str, set[int]] = {}
        for src, t, e, i in ext:
            if src == OLIMP and t == "team":
                by_name.setdefault(e.split("|", 1)[1], set()).add(i)
        self.archive_team = {n: next(iter(ids)) for n, ids in by_name.items() if len(ids) == 1}
        self.leagues = {(c, g, a, norm(n)): i for i, c, g, a, n in s.execute(
            select(League.id, League.country, League.gender, League.age_group, League.name)
            .where(League.sport_id == SPORT))}
        self.renamed: set[int] = set()

    def _map(self, etype: str, key: str, name: str, entity_id: int, method: str) -> int:
        self.s.add(SourceEntity(source_id=SOURCE, entity_type=etype, external_id=key, external_name=name[:255],
                                entity_id=entity_id, method=method))
        self.own[(etype, key)] = entity_id
        return entity_id

    def league(self, tid: int, en_name: str) -> int:
        if ("league", str(tid)) in self.own:
            return self.own[("league", str(tid))]
        ru = self.names_ru.get(("tournament", tid))
        if ru:
            if f"Футбол. {ru}" in self.archive_league:
                return self._map("league", str(tid), en_name, self.archive_league[f"Футбол. {ru}"], "ru_name")
            p = parse_tournament(f"Футбол. {ru}")
            base = split_stage("x. " + p["name"])[0][3:]  # 'x. ' lets the first part be a stage too
            key = (p["country"], p["gender"], p["age_group"], norm(base))
            if key in self.leagues:
                return self._map("league", str(tid), en_name, self.leagues[key], "ru_name")
        e = en_tournament(en_name)
        key = (e["country"], e["gender"], e["age_group"], norm(e["name"]))
        if key not in self.leagues:
            lg = League(sport_id=SPORT, country=e["country"], name=e["name"], gender=e["gender"],
                        age_group=e["age_group"], kind="international" if e["country"] is None else
                        "cup" if re.search(r"\bCup\b|Copa|Coupe|Pokal|Coppa", e["name"]) else "league")
            self.s.add(lg)
            self.s.flush()
            self.leagues[key] = lg.id
        return self._map("league", str(tid), en_name, self.leagues[key], "provider")

    def team(self, cid: int, en_name: str, country: str | None, gender: str, age: str | None) -> int:
        if ("team", str(cid)) in self.own:
            return self.own[("team", str(cid))]
        ru = self.names_ru.get(("competitor", cid))
        tid = self.archive_team.get(ru) if ru else None
        if tid:
            if tid not in self.renamed:
                self.s.execute(update(Team).where(Team.id == tid, Team.name.op("~")("[А-Яа-яЁё]"))
                               .values(name=en_name[:160]))
                self.renamed.add(tid)
            return self._map("team", str(cid), en_name, tid, "ru_name")
        t = Team(sport_id=SPORT, name=en_name[:160], country=country, kind="club", gender=gender, age_group=age)
        self.s.add(t)
        self.s.flush()
        return self._map("team", str(cid), en_name, t.id, "provider")


def existing_index(s, since: dt.datetime) -> dict[tuple[int, int, dt.date], int]:
    rows = s.execute(select(Match.id, Match.home_team_id, Match.away_team_id, Match.start_time)
                     .where(Match.sport_id == SPORT, Match.start_time >= since))
    return {(h, a, st.date()): mid for mid, h, a, st in rows}


def main() -> None:
    src = sqlite3.connect(f"file:{fd.DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    names_ru = {(k, i): n for k, i, n in src.execute("SELECT kind, id, name FROM ob_fb_names_ru")}
    first = src.execute("SELECT MIN(date) FROM ob_fb_events WHERE kind = 'GENERAL' AND hs IS NOT NULL").fetchone()[0]
    since = dt.datetime.fromisoformat(first.replace("Z", "+00:00")) - dt.timedelta(days=2)
    total = src.execute("SELECT COUNT(*) FROM ob_fb_events WHERE kind = 'GENERAL' AND event_type = 'MAIN' "
                        "AND hs IS NOT NULL").fetchone()[0]
    with session() as s:
        done = set(s.scalars(select(SourceEntity.external_id).where(SourceEntity.source_id == SOURCE,
                                                                    SourceEntity.entity_type == "match")))
        index = existing_index(s, since)
    print(f"Olimpbet API football -> PostgreSQL: {total} scored matches from {first[:10]}, {len(done)} done, "
          f"{len(index)} matches in the database since then to link against", flush=True)
    cur = src.execute("SELECT * FROM ob_fb_events WHERE kind = 'GENERAL' AND event_type = 'MAIN' AND hs IS NOT NULL "
                      "ORDER BY date")
    t0, seen, cnt = time.time(), 0, Counter()
    while True:
        rows = cur.fetchmany(CHUNK)
        if not rows:
            break
        seen += len(rows)
        with session() as s:
            refs = Refs(s, names_ru)
            new, new_rows, mapping = [], [], []
            for r in rows:
                if str(r["id"]) in done or PSEUDO.search(r["tournament"] or ""):
                    cnt["skipped"] += 1
                    continue
                e = en_tournament(r["tournament"])
                league_id = refs.league(r["tournament_id"], r["tournament"])
                home = refs.team(r["home_id"], r["home"], e["country"], e["gender"], e["age_group"])
                away = refs.team(r["away_id"], r["away"], e["country"], e["gender"], e["age_group"])
                start = dt.datetime.fromisoformat(r["date"].replace("Z", "+00:00"))
                done.add(str(r["id"]))
                hit = next((index[k] for d in (0, -1, 1)
                            if (k := (home, away, start.date() + dt.timedelta(days=d))) in index), None)
                if hit:
                    mapping.append((r, hit, "linked"))
                    continue
                periods = json.loads(r["periods"]) if r["periods"] else None
                new.append({"sport_id": SPORT, "league_id": league_id, "stage": (e["stage"] or None) and
                            e["stage"][:64], "start_time": start, "home_team_id": home, "away_team_id": away,
                            "status": "finished", "home_score": r["hs"], "away_score": r["as_"],
                            "periods": periods,
                            "result_extra": {k: r[k] for k in ("home_red", "away_red", "add_minutes")
                                             if r[k] is not None} or None})
                new_rows.append(r)
            ids = s.scalars(insert(Match).returning(Match.id, sort_by_parameter_order=True), new).all() \
                if new else []
            mapping += [(r, mid, "provider") for r, mid in zip(new_rows, ids)]
            if mapping:
                s.execute(insert(SourceEntity), [
                    {"source_id": SOURCE, "entity_type": "match", "external_id": str(r["id"]),
                     "external_name": f"{r['home']} - {r['away']}"[:255], "entity_id": mid, "method": method}
                    for r, mid, method in mapping])
                # statistics events of these matches
                base = {r["id"]: mid for r, mid, _ in mapping}
                stats = []
                ids_sql = ",".join(str(i) for i in base)
                for st in src.execute(f"SELECT base_event_id, event_type, hs, as_, periods FROM ob_fb_events "
                                      f"WHERE kind = 'STATISTICS' AND hs IS NOT NULL AND base_event_id IN ({ids_sql})"):
                    stat = STATS.get(st["event_type"])
                    if not stat:
                        continue
                    mid = base[st["base_event_id"]]
                    stats.append({"match_id": mid, "source_id": SOURCE, "stat": stat, "period": 0,
                                  "home_value": st["hs"], "away_value": st["as_"]})
                    for i, (h, a) in enumerate(json.loads(st["periods"] or "[]"), 1):
                        if h is not None and a is not None:
                            stats.append({"match_id": mid, "source_id": SOURCE, "stat": stat, "period": i,
                                          "home_value": h, "away_value": a})
                if stats:
                    s.execute(pg_insert(MatchStatistic).on_conflict_do_nothing(), stats)
                cnt["stats"] += len(stats)
            cnt["new"] += len(ids)
            cnt["linked"] += sum(1 for _, _, m in mapping if m == "linked")
        eta = (time.time() - t0) / seen * (total - seen)
        print(f"[{seen:7d}/{total}] {rows[-1]['date'][:7]}  new {cnt['new']:7d}  linked {cnt['linked']:6d}  "
              f"stats {cnt['stats']:7d}  skipped {cnt['skipped']:5d}   ETA {int(eta // 60)}m{int(eta % 60):02d}s",
              flush=True)
    with session() as s:
        teams = s.execute(text("SELECT method, COUNT(*) FROM source_entities WHERE source_id = :s "
                               "AND entity_type = 'team' GROUP BY 1"), {"s": SOURCE}).all()
    print(f"done in {(time.time() - t0) / 60:.1f} min: {dict(cnt)}; teams {dict(teams)}")


if __name__ == "__main__":
    main()
