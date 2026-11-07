"""Olimpbet results raw_payloads (data_engine/providers/olimpbet_results.py: kind 'results_en', one row per sport and
day) -> matches of football, tennis, basketball, hockey, volleyball.

- match = a GENERAL / MAIN event with a score; statistics events (kind STATISTICS: corners, aces, 3 pointers,
  faceoffs ...) point to it by baseEventId and go to match_statistics (whole match = period 0, then by period);
- scores: football / basketball = goals / points (basketball includes overtime, the periods show it); hockey
  currentScore is regulation time only, so overtime (period 4) and the shootout (period 5, winner +1) are added
  and the regulation score kept in result_extra; tennis / volleyball = sets, periods = games / points of a set;
- football keeps the rules of data_engine/legacy/olimpbet_football.py (archive teams by Russian name, statistics
  names, pseudo tournaments); the other sports get their own teams (tennis: player / pair) and leagues;
- a match already in the database (same teams, +-1 day: football-data, Polymarket ...) is linked, and gets the
  result if it has none yet.
Re-run skips events already imported. Then Polymarket matches / teams are linked (polymarket_links).

  python -u -m data_engine.normalization.olimpbet_results [--sports tennis,hockey] [--days 5]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
from collections import Counter, defaultdict

from sqlalchemy import insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db import session
from core.models import League, Match, MatchStatistic, RawPayload, SourceEntity, Team
from data_engine.legacy import olimpbet_football as obf
from data_engine.normalization import polymarket_links
from data_engine.normalization.olimp_cleanup import norm
from pmbot import olimpbet as ob

SOURCE = 3  # olimpbet
SPORT_IDS = {"football": 1, "tennis": 2, "basketball": 3, "hockey": 5, "volleyball": 4}
BATCH_DAYS = 7
STAGE = re.compile(
    r"^(\d+(st|nd|rd|th)\b|1/\d+ finals?|(semi|quarter)[- ]?finals?|finals?\b|group\b|groups\b|league phase|round\b|"
    r"round of|qualification|qualifying|preliminary|play-?offs?|play-?outs?|play-?in|knockout|first leg|"
    r"second leg|matchday|tour \d|best of|regular season|stage\b|final stage|placement|classification|"
    r"3rd place|relegation|promotion|super final)", re.IGNORECASE)
INTERNATIONAL = re.compile(
    r"^(FIBA|CEV|IIHF|Champions|Euro|World|Asian|Asia|African|Africa|Pan American|Olympic|Friendly|Club Friendly|"
    r"International|Baltic|Adriatic|ABA|VTB|Balkan|Alps|Continental|Nordic|South America|Southeast Asian|"
    r"Caribbean|Central American|ECVA|Gulf|Arab|BSKT|Universiade)", re.IGNORECASE)
# leagues Olimpbet names without a country, as Polymarket / football-data know them
LEAGUE_COUNTRY = {"NBA": "USA", "WNBA": "USA", "NHL": "USA", "AHL": "USA", "NCAA": "USA", "NBA G League": "USA",
                  "G League": "USA"}
STAT_NAMES = {"2 pointer": "two_pointers", "3 pointer": "three_pointers", "Faceoff": "faceoffs",
              "Number of two-minute penalties": "penalties_2min", "Power play goals": "pp_goals",
              "Free throws": "free_throws", **obf.STATS}
SKIP_STATS = re.compile(r"special|battle|head-to-head", re.IGNORECASE)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:32]


def tournament(sport: str, name: str) -> dict:
    """'Russia. Superliga. Semi-Finals. Best of 5. Women' -> country, league name, stage, gender, age, kind."""
    name = re.sub(r"^OLIMPBET\s*-\s*", "", name or "").strip()
    parts = [p.strip() for p in name.split(". ") if p.strip()]
    gender = "f" if any(p.lower() == "women" for p in parts) else "m"
    parts = [p for p in parts if p.lower() != "women"] or ["?"]
    cut = next((i for i, p in enumerate(parts) if i > 0 and STAGE.match(p)), len(parts))
    base, stage = parts[:cut], ". ".join(parts[cut:]) or None
    age = re.search(r"\bU-?(\d\d)\b", " ".join(base))
    if sport == "tennis":
        t = ob.parse_tournament(name)
        gender = "x" if re.search(r"\bmixed\b", name, re.IGNORECASE) else "f" if t["women"] else "m"
        return {"country": None, "name": ". ".join(base)[:160], "stage": stage, "gender": gender,
                "age_group": f"U{age[1]}" if age else None, "kind": "tournament"}
    international = len(base) == 1 or bool(INTERNATIONAL.match(base[0]))
    league = ". ".join(base if international else base[1:])
    country = LEAGUE_COUNTRY.get(league, None if international else base[0])
    kind = ("friendly" if re.search(r"friendl", league, re.IGNORECASE) else
            "cup" if re.search(r"\bCup\b|Copa|Coupe|Pokal|Coppa|Kupa", league) else
            "international" if country is None else "league")
    return {"country": country, "name": league[:160], "stage": stage and stage[:64], "gender": gender,
            "age_group": f"U{age[1]}" if age else None, "kind": kind}


class Refs:
    """Olimpbet tournament / competitor ids -> our leagues / teams (non-football sports)."""

    def __init__(self, s, sport_id: int) -> None:
        self.s = s
        self.sport_id = sport_id
        self.own = {(t, e): i for t, e, i in s.execute(
            select(SourceEntity.entity_type, SourceEntity.external_id, SourceEntity.entity_id)
            .where(SourceEntity.source_id == SOURCE, SourceEntity.entity_type.in_(("league", "team"))))}
        self.leagues = {(c, g, a, norm(n)): i for i, c, g, a, n in s.execute(
            select(League.id, League.country, League.gender, League.age_group, League.name)
            .where(League.sport_id == sport_id))}

    def _map(self, etype: str, key: str, name: str, entity_id: int, method: str = "provider") -> int:
        self.s.add(SourceEntity(source_id=SOURCE, entity_type=etype, external_id=key, external_name=name[:255],
                                entity_id=entity_id, method=method))
        self.own[(etype, key)] = entity_id
        return entity_id

    def league(self, tid: int, raw_name: str, t: dict) -> int:
        if ("league", str(tid)) in self.own:
            return self.own[("league", str(tid))]
        key = (t["country"], t["gender"], t["age_group"], norm(t["name"]))
        method = "name" if key in self.leagues else "provider"
        if key not in self.leagues:
            lg = League(sport_id=self.sport_id, country=t["country"], name=t["name"], gender=t["gender"],
                        age_group=t["age_group"], kind=t["kind"])
            self.s.add(lg)
            self.s.flush()
            self.leagues[key] = lg.id
        return self._map("league", str(tid), raw_name, self.leagues[key], method)

    def team(self, cid: int, name: str, t: dict) -> int:
        if ("team", str(cid)) in self.own:
            return self.own[("team", str(cid))]
        tennis = self.sport_id == SPORT_IDS["tennis"]
        kind = ("pair" if "/" in name else "player") if tennis else "club"
        gender = "f" if re.search(r"\((W|Women)\)", name) else t["gender"]
        age = re.search(r"\(U-?(\d\d)\)", name)
        team = Team(sport_id=self.sport_id, name=name[:160], country=t["country"] if not tennis else None,
                    kind=kind, gender=gender, age_group=f"U{age[1]}" if age else t["age_group"])
        self.s.add(team)
        self.s.flush()
        return self._map("team", str(cid), name, team.id)


def hockey_score(hs: int, as_: int, per: list[list[int]]) -> tuple[int, int, dict]:
    """currentScore is regulation; period 4 = overtime, period 5 = shootout (its winner gets +1)."""
    def tot(ps):
        return sum(p[0] for p in ps), sum(p[1] for p in ps)
    k = 3 if len(per) >= 3 and tot(per[:3]) == (hs, as_) else len(per) if tot(per) == (hs, as_) else None
    if k is None or k >= len(per):
        return hs, as_, ({"overtime": True} if k is not None and k > 3 else {})
    extra: dict = {"regulation": [hs, as_], "overtime": True}
    hs, as_ = hs + per[k][0], as_ + per[k][1]
    if len(per) > k + 1 and hs == as_:
        so = per[k + 1]
        extra["shootout"] = so
        hs, as_ = hs + (so[0] > so[1]), as_ + (so[1] > so[0])
    if hs == as_:
        extra["unresolved"] = True
    return hs, as_, extra


def result(sport: str, item: dict) -> dict | None:
    """Score fields of a finished match; None without a score."""
    b = item.get("basicStatistics") or {}
    cur = b.get("currentScore") or {}
    hs, as_ = cur.get("homeScore"), cur.get("awayScore")
    if hs is None or as_ is None:
        return None
    per = [[p.get("homeScore"), p.get("awayScore")] for p in b.get("scoresByPeriod") or []]
    per = [p for p in per if None not in p]
    extra: dict = {}
    status = "finished"
    if sport == "hockey":
        hs, as_, extra = hockey_score(hs, as_, per)
    elif sport == "basketball":
        while len(per) > 4 and per[-1] == [0, 0]:
            per.pop()
        if len(per) > 4:
            extra["overtime"] = len(per) - 4
    elif sport in ("tennis", "volleyball"):
        if hs == as_ == 0 and not per:
            status, hs, as_ = "walkover", None, None
        elif max(hs, as_) < 2:  # retired / stopped before the end
            extra["incomplete"] = True
    elif sport == "football":
        live = {x.get("code"): x.get("value") for x in item.get("liveStatistics") or []}
        for key, code in (("home_red", "team1_red_cards"), ("away_red", "team2_red_cards")):
            if ob._int(live.get(code)) is not None:
                extra[key] = ob._int(live.get(code))
        if live.get("add_minutes"):
            extra["add_minutes"] = live["add_minutes"]
    return {"status": status, "home_score": hs, "away_score": as_, "periods": per or None,
            "result_extra": extra or None}


def sides(item: dict) -> tuple[dict, dict] | None:
    comps = {c["id"]: c["name"] for c in item.get("competitors") or []}
    if len(comps) != 2:
        return None
    home_ids = item.get("homeCompetitorIds") or []
    home = home_ids[0] if home_ids and home_ids[0] in comps else next(iter(comps))
    away = next(i for i in comps if i != home)
    return {"id": home, "name": comps[home]}, {"id": away, "name": comps[away]}


def football_names_ru() -> dict[tuple[str, int], str]:
    with session() as s:
        rows = s.execute(text("""
            SELECT 'competitor', (c->>'id')::bigint, c->>'name' FROM raw_payloads r,
              jsonb_array_elements(r.payload->'items') i, jsonb_array_elements(i->'competitors') c
            WHERE r.source_id = :src AND r.kind = 'results_ru' AND r.external_id LIKE 'football|%'
            UNION
            SELECT 'tournament', (i->'tournament'->>'id')::bigint, i->'tournament'->>'name' FROM raw_payloads r,
              jsonb_array_elements(r.payload->'items') i
            WHERE r.source_id = :src AND r.kind = 'results_ru' AND r.external_id LIKE 'football|%'"""),
                         {"src": SOURCE}).all()
    return {(k, i): n for k, i, n in rows if n}


def existing(s, sport_id: int, lo: dt.datetime, hi: dt.datetime) -> dict[tuple[int, int, dt.date], tuple]:
    """Matches of other sources (football-data, Polymarket ...) to link: Olimpbet's own are never linked - in
    amateur leagues the same teams meet again the next day."""
    rows = s.execute(text("""
        SELECT m.id, m.home_team_id, m.away_team_id, m.start_time, m.status FROM matches m
        WHERE m.sport_id = :sp AND m.start_time BETWEEN :lo AND :hi AND NOT EXISTS (
          SELECT 1 FROM source_entities se WHERE se.entity_type = 'match' AND se.entity_id = m.id
            AND se.source_id = :src)"""), {"sp": sport_id, "lo": lo, "hi": hi, "src": SOURCE})
    return {(h, a, st.astimezone(dt.timezone.utc).date()): (mid, status) for mid, h, a, st, status in rows}


def normalize(sport: str, since: dt.date | None, done: set[str]) -> Counter:
    sport_id = SPORT_IDS[sport]
    with session() as s:
        q = select(RawPayload.external_id).where(RawPayload.source_id == SOURCE, RawPayload.kind == "results_en",
                                                 RawPayload.external_id.like(f"{sport}|%"))
        keys = sorted(k for k in s.scalars(q) if not since or k.split("|")[1] >= since.isoformat())
        refs = obf.Refs(s, football_names_ru()) if sport == "football" else Refs(s, sport_id)
    cnt: Counter = Counter()
    t0 = time.time()
    print(f"{sport}: {len(keys)} days ({keys[0].split('|')[1] if keys else '-'} .."
          f" {keys[-1].split('|')[1] if keys else '-'})", flush=True)
    for b in range(0, len(keys), BATCH_DAYS):
        part = keys[b:b + BATCH_DAYS]
        with session() as s:
            refs.s = s
            items = [i for p in s.scalars(select(RawPayload.payload).where(
                RawPayload.source_id == SOURCE, RawPayload.kind == "results_en", RawPayload.external_id.in_(part)))
                for i in p["items"]]
            stats_of = defaultdict(list)
            for i in items:
                if i.get("kind") == "STATISTICS" and i.get("baseEventId"):
                    stats_of[i["baseEventId"]].append(i)
            mains = [i for i in items if i.get("kind") == "GENERAL" and i.get("eventType") == "MAIN"
                     and str(i["id"]) not in done]
            if not mains:
                continue
            dates = [dt.datetime.fromisoformat(i["eventDate"].replace("Z", "+00:00")) for i in mains]
            index = existing(s, sport_id, min(dates) - dt.timedelta(days=2), max(dates) + dt.timedelta(days=2))
            new, new_items, mapping, fills = [], [], [], []
            for i, start in zip(mains, dates):
                tname = (i.get("tournament") or {}).get("name") or ""
                pair, res = sides(i), result(sport, i)
                if not pair or not res or obf.PSEUDO.search(tname):
                    cnt["skipped"] += 1
                    continue
                home_c, away_c = pair
                tid = i["tournament"]["id"]
                if sport == "football":
                    t = obf.en_tournament(tname)
                    league = refs.league(tid, tname)
                    home = refs.team(home_c["id"], home_c["name"], t["country"], t["gender"], t["age_group"])
                    away = refs.team(away_c["id"], away_c["name"], t["country"], t["gender"], t["age_group"])
                else:
                    t = tournament(sport, tname)
                    league = refs.league(tid, tname, t)
                    home, away = refs.team(home_c["id"], home_c["name"], t), refs.team(away_c["id"], away_c["name"], t)
                done.add(str(i["id"]))
                hit = next((index[k] for d in (0, -1, 1)
                            if (k := (home, away, start.date() + dt.timedelta(days=d))) in index), None)
                if hit:
                    mapping.append((i, hit[0], "linked"))
                    if hit[1] != "finished":
                        fills.append({"id": hit[0], **res, "stage": t["stage"] and t["stage"][:64], "league": league})
                    continue
                new.append({"sport_id": sport_id, "league_id": league, "stage": t["stage"] and t["stage"][:64],
                            "start_time": start, "home_team_id": home, "away_team_id": away, **res})
                new_items.append(i)
            ids = s.scalars(insert(Match).returning(Match.id, sort_by_parameter_order=True), new).all() \
                if new else []
            mapping += [(i, mid, "provider") for i, mid in zip(new_items, ids)]
            for f in fills:
                s.execute(text("UPDATE matches SET status = :status, home_score = :home_score, away_score = "
                               ":away_score, periods = CAST(:periods AS jsonb), result_extra = CAST(:result_extra "
                               "AS jsonb), stage = COALESCE(stage, :stage), league_id = :league WHERE id = :id"),
                          {**f, "periods": json.dumps(f["periods"]) if f["periods"] else None,
                           "result_extra": json.dumps(f["result_extra"]) if f["result_extra"] else None})
            if mapping:
                s.execute(insert(SourceEntity), [
                    {"source_id": SOURCE, "entity_type": "match", "external_id": str(i["id"]),
                     "external_name": " - ".join(c["name"] for c in i["competitors"])[:255], "entity_id": mid,
                     "method": method} for i, mid, method in mapping])
            stats = []
            for i, mid, _ in mapping:
                for st in stats_of.get(i["id"], []):
                    etype = st.get("eventType") or ""
                    cur = (st.get("basicStatistics") or {}).get("currentScore") or {}
                    if SKIP_STATS.search(etype) or cur.get("homeScore") is None or cur.get("awayScore") is None:
                        continue
                    stat = STAT_NAMES.get(etype) or slug(etype)
                    stats.append({"match_id": mid, "source_id": SOURCE, "stat": stat, "period": 0,
                                  "home_value": cur["homeScore"], "away_value": cur["awayScore"]})
                    for n, p in enumerate((st.get("basicStatistics") or {}).get("scoresByPeriod") or [], 1):
                        if p.get("homeScore") is not None and p.get("awayScore") is not None:
                            stats.append({"match_id": mid, "source_id": SOURCE, "stat": stat, "period": n,
                                          "home_value": p["homeScore"], "away_value": p["awayScore"]})
            for c in range(0, len(stats), 20000):
                s.execute(pg_insert(MatchStatistic).on_conflict_do_nothing(), stats[c:c + 20000])
            cnt["new"] += len(ids)
            cnt["linked"] += sum(1 for _, _, m in mapping if m == "linked")
            cnt["filled"] += len(fills)
            cnt["stats"] += len(stats)
        done_days = min(b + BATCH_DAYS, len(keys))
        eta = (time.time() - t0) / done_days * (len(keys) - done_days)
        print(f"  [{done_days:4d}/{len(keys)}] {part[-1].split('|')[1]}  new {cnt['new']:7d}  linked "
              f"{cnt['linked']:6d}  filled {cnt['filled']:5d}  stats {cnt['stats']:8d}  skipped {cnt['skipped']:6d}"
              f"   ETA {int(eta // 60)}m{int(eta % 60):02d}s", flush=True)
    return cnt


def fix_tennis_gender(s) -> int:
    """A player's gender = that of most of their leagues (the first tournament may be mixed / misnamed)."""
    return s.execute(text("""
        WITH side AS (SELECT home_team_id tid, league_id FROM matches WHERE sport_id = 2
                      UNION ALL SELECT away_team_id, league_id FROM matches WHERE sport_id = 2),
             g AS (SELECT side.tid id, mode() WITHIN GROUP (ORDER BY l.gender) g FROM side
                   JOIN leagues l ON l.id = side.league_id WHERE l.gender IN ('m', 'f') GROUP BY side.tid)
        UPDATE teams t SET gender = g.g FROM g WHERE t.id = g.id AND t.gender <> g.g AND t.kind <> 'pair'""")).rowcount


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sports", default=",".join(SPORT_IDS))
    ap.add_argument("--days", type=int, help="only the raw days of the last N days (default: all)")
    ap.add_argument("--no-links", action="store_true", help="skip linking Polymarket matches")
    args = ap.parse_args()
    since = dt.date.today() - dt.timedelta(days=args.days) if args.days else None
    with session() as s:
        done = set(s.scalars(select(SourceEntity.external_id).where(SourceEntity.source_id == SOURCE,
                                                                    SourceEntity.entity_type == "match")))
    t0 = time.time()
    for sport in [x.strip() for x in args.sports.split(",") if x.strip()]:
        print(f"{sport}: {dict(normalize(sport, since, done))}", flush=True)
    with session() as s:
        print(f"tennis players with the gender fixed: {fix_tennis_gender(s)}")
    if not args.no_links:
        polymarket_links.main()
    print(f"done in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
