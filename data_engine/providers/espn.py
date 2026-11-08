"""ESPN site API (site.api.espn.com, the public JSON of espn.com: scoreboard + game summary) -> PostgreSQL: results
with scores by period and team box score statistics of NBA, WNBA, NHL and the European cups / Nations League /
EFL Cup - the leagues of the Polymarket series that football-data and the Olimpbet window (2025-09 ..) miss.
ESPN has no odds of past games.

1. fetch: scoreboard day by day (ranges are refused) + summary box score of every finished game -> raw_payloads
   (kind 'espn_month', external_id 'nba|2025-01', events trimmed to ids, teams, scores, periods, team stats);
2. normalize: a game already in the database (same teams +-1 day; or both sides with similar names, which maps the
   ESPN teams by vote) is linked and gets the result if it has none, else a match is created; team stats go to
   match_statistics ('fieldGoalsMade-fieldGoalsAttempted' 46-86 -> two stats). NHL: the score is final (shootout
   winner +1), the regulation score is kept in result_extra.

  python -u -m data_engine.providers.espn [--since 2024-11-01] [--leagues nba,nhl]
  python -u -m data_engine.providers.espn --recent 2      # hourly: today + yesterday
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from collections import defaultdict

import httpx
from sqlalchemy import delete, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db import session
from core.models import (League, Match, MatchEvent, MatchPlayer, MatchStatistic, Player, RawPayload,
                         SourceEntity, Team)
from data_engine.normalization.polymarket_links import similar, words
from data_engine.providers.polymarket_prices import norm_name
from pmbot.http import HttpClient

SOURCE = 9  # espn
API = "https://site.api.espn.com/apis/site/v2/sports"
# code -> (espn path, sport id, our league (country, name), gender, football-data league code or None)
LEAGUES = {
    "nba": ("basketball/nba", 3, ("USA", "NBA"), "m", None),
    "wnba": ("basketball/wnba", 3, ("USA", "WNBA"), "f", None),
    "nhl": ("hockey/nhl", 5, ("USA", "NHL"), "m", None),
    "ucl": ("soccer/uefa.champions", 1, (None, "UEFA Champions League"), "m", None),
    "uel": ("soccer/uefa.europa", 1, (None, "UEFA Europa League"), "m", None),
    "uecl": ("soccer/uefa.europa.conf", 1, (None, "UEFA Conference League"), "m", None),
    "unl": ("soccer/uefa.nations", 1, (None, "UEFA Nations League"), "m", None),
    "efl": ("soccer/eng.league_cup", 1, ("England", "EFL Cup"), "m", None),
    # domestic leagues of the Polymarket series (+ neighbours): lineups / player stats for the existing matches
    "epl": ("soccer/eng.1", 1, ("England", "Premier League"), "m", "E0"),
    "champ": ("soccer/eng.2", 1, ("England", "Championship"), "m", "E1"),
    "laliga": ("soccer/esp.1", 1, ("Spain", "La Liga"), "m", "SP1"),
    "seriea": ("soccer/ita.1", 1, ("Italy", "Serie A"), "m", "I1"),
    "bundesliga": ("soccer/ger.1", 1, ("Germany", "Bundesliga"), "m", "D1"),
    "ligue1": ("soccer/fra.1", 1, ("France", "Ligue 1"), "m", "F1"),
    "eredivisie": ("soccer/ned.1", 1, ("Netherlands", "Eredivisie"), "m", "N1"),
    "primeira": ("soccer/por.1", 1, ("Portugal", "Primeira Liga"), "m", "P1"),
    "mls": ("soccer/usa.1", 1, ("USA", "MLS"), "m", "USA"),
    "brasileirao": ("soccer/bra.1", 1, ("Brazil", "Serie A"), "m", "BRA"),
    "ligamx": ("soccer/mex.1", 1, ("Mexico", "Liga MX"), "m", "MEX"),
}
EVENT_TYPES = {"goal": "goal", "goal - header": "goal", "goal - free-kick": "goal", "goal - volley": "goal",
               "penalty - scored": "penalty_goal", "own goal": "own_goal", "yellow card": "yellow",
               "red card": "red", "substitution": "sub"}


def months(since: dt.date, until: dt.date) -> list[tuple[dt.date, dt.date]]:
    out, d = [], since.replace(day=1)
    while d <= until:
        nxt = (d + dt.timedelta(days=32)).replace(day=1)
        out.append((max(d, since), min(nxt - dt.timedelta(days=1), until)))
        d = nxt
    return out


def box(summary: dict) -> dict[str, list[dict]]:
    """team id -> [{name, value}] of the box score."""
    out = {}
    for t in (summary.get("boxscore") or {}).get("teams") or []:
        out[str(t["team"]["id"])] = [{"name": x.get("name"), "value": x.get("displayValue")}
                                     for x in t.get("statistics") or []]
    return out


def players_of(summary: dict) -> dict[str, list[dict]]:
    """team id -> players of the game: football from the rosters (starter / bench, position, player stats),
    basketball / hockey from the player box score (starter, did not play + reason, stats by column)."""
    out: dict[str, list[dict]] = {}
    for r in summary.get("rosters") or []:
        tid = str((r.get("team") or {}).get("id"))
        out[tid] = [{"id": str(p["athlete"]["id"]), "name": p["athlete"].get("displayName"),
                     "pos": (p.get("position") or {}).get("abbreviation"), "starter": bool(p.get("starter")),
                     "sub_in": bool(p.get("subbedIn")), "sub_out": bool(p.get("subbedOut")),
                     "stats": {x["name"]: x.get("value") for x in p.get("stats") or [] if x.get("name")}}
                    for p in r.get("roster") or [] if (p.get("athlete") or {}).get("id")]
    for t in (summary.get("boxscore") or {}).get("players") or []:
        tid = str((t.get("team") or {}).get("id"))
        rows = []
        for block in t.get("statistics") or []:
            names = block.get("names") or block.get("labels") or []
            for a in block.get("athletes") or []:
                if not (a.get("athlete") or {}).get("id"):  # team rows / placeholders without an athlete id
                    continue
                rows.append({"id": str(a["athlete"]["id"]), "name": a["athlete"].get("displayName"),
                             "pos": ((a["athlete"].get("position") or {}).get("abbreviation")),
                             "starter": bool(a.get("starter")), "dnp": bool(a.get("didNotPlay")),
                             "reason": (a.get("reason") or None) if a.get("didNotPlay") else None,
                             "stats": dict(zip(names, a.get("stats") or []))})
        if rows:
            out[tid] = rows
    return out


def events_of(summary: dict) -> list[dict]:
    """Football key events: goals / cards / substitutions with the minute, team and player."""
    out = []
    for e in summary.get("keyEvents") or []:
        kind = EVENT_TYPES.get(((e.get("type") or {}).get("text") or "").lower())
        if not kind:
            continue
        minute = re.match(r"(\d+)", (e.get("clock") or {}).get("displayValue") or "")
        out.append({"type": kind, "minute": int(minute[1]) if minute else None,
                    "team": str((e.get("team") or {}).get("id")) if e.get("team") else None,
                    "players": [str(p["athlete"]["id"]) for p in e.get("participants") or []
                                if (p.get("athlete") or {}).get("id")]})
    return out


def summary_of(http: HttpClient, path: str, event_id: str) -> dict:
    try:
        s = http.get_json(f"{API}/{path}/summary", {"event": event_id})
    except Exception:  # summary missing: the result is still useful
        return {"stats": {}, "players": {}, "events": []}
    out = {}
    for key, parse in (("stats", box), ("players", players_of), ("events", events_of)):
        try:
            out[key] = parse(s)
        except Exception as e:  # one odd summary must not stop a backfill; the other parts are kept
            print(f"  summary {event_id}: {key} not parsed ({e!r:.80})", flush=True)
            out[key] = [] if key == "events" else {}
    return out


def team_name(name: str | None) -> str | None:
    """'UEFA Nations League: Netherlands' / 'UCL: Real Madrid' (ESPN's placeholder names) -> 'Netherlands'."""
    return re.sub(r"^(UEFA [A-Za-z ]+League|UCL|UEL|UECL):\s*", "", name) if name else name


def compact(e: dict, detail: dict) -> dict:
    c = e["competitions"][0]
    stats, players = detail.get("stats") or {}, detail.get("players") or {}
    return {"id": e["id"], "date": e["date"], "status": c["status"]["type"].get("name"),
            "completed": c["status"]["type"].get("completed"), "neutral": c.get("neutralSite"),
            "season": (e.get("season") or {}).get("year"), "season_type": (e.get("season") or {}).get("type"),
            "note": (c.get("notes") or [{}])[0].get("headline") if c.get("notes") else None,
            "teams": [{"id": t["team"]["id"], "name": team_name(t["team"].get("displayName")),
                       "short": t["team"].get("shortDisplayName"), "abbr": t["team"].get("abbreviation"),
                       "location": t["team"].get("location"), "side": t.get("homeAway"), "score": t.get("score"),
                       "winner": t.get("winner"), "shootout": t.get("shootoutScore"),
                       "periods": [x.get("value") for x in t.get("linescores") or []],
                       "stats": stats.get(str(t["team"]["id"])),
                       "players": players.get(str(t["team"]["id"]))} for t in c["competitors"]],
            "events": detail.get("events") or []}


def fetch(http: HttpClient, code: str, lo: dt.date, hi: dt.date) -> list[dict]:
    """Games of the days lo .. hi (the scoreboard takes one day; date ranges are refused)."""
    path = LEAGUES[code][0]
    out, seen = [], set()
    day_events = []
    d = lo
    while d <= hi:
        day_events += http.get_json(f"{API}/{path}/scoreboard", {"dates": f"{d:%Y%m%d}", "limit": 300}).get(
            "events") or []
        d += dt.timedelta(days=1)
    for e in day_events:
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        c = e["competitions"][0]
        detail = summary_of(http, path, e["id"]) if c["status"]["type"].get("completed") else {}
        out.append(compact(e, detail))
    return out


def update_days(http: HttpClient, code: str, days: list[dt.date]) -> list[str]:
    """Hourly: the scoreboards of these days merged into their month payloads; a box score is fetched only for a
    game that finished since the last run. Returns the month keys touched."""
    path = LEAGUES[code][0]
    by_month = defaultdict(list)
    for d in days:
        by_month[f"{code}|{d:%Y-%m}"].append(d)
    for key, month_days in by_month.items():
        with session() as s:
            stored = s.scalar(select(RawPayload.payload).where(RawPayload.source_id == SOURCE,
                                                               RawPayload.kind == "espn_month",
                                                               RawPayload.external_id == key))
        events = {e["id"]: e for e in (stored or {}).get("events", [])}
        for d in month_days:
            for e in http.get_json(f"{API}/{path}/scoreboard", {"dates": f"{d:%Y%m%d}", "limit": 300}).get(
                    "events") or []:
                prev = events.get(e["id"])
                completed = e["competitions"][0]["status"]["type"].get("completed")
                if prev and prev["completed"] and any(t.get("players") for t in prev["teams"]):
                    continue  # finished and complete already
                events[e["id"]] = compact(e, summary_of(http, path, e["id"]) if completed else {})
        with session() as s:
            s.execute(delete(RawPayload).where(RawPayload.source_id == SOURCE, RawPayload.kind == "espn_month",
                                               RawPayload.external_id == key))
            s.execute(insert(RawPayload).values(source_id=SOURCE, kind="espn_month", external_id=key,
                                                payload={"events": sorted(events.values(), key=lambda e: e["date"])}))
    return list(by_month)


def num(v) -> float | None:
    try:
        return float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return None


def stat_rows(match_id: int, home: dict, away: dict) -> list[dict]:
    rows = []
    hs = {x["name"]: x["value"] for x in home.get("stats") or []}
    as_ = {x["name"]: x["value"] for x in away.get("stats") or []}
    for name, hv in hs.items():
        av = as_.get(name)
        if av is None or not name:
            continue
        names = name.split("-")
        hv_parts, av_parts = str(hv).split("-"), str(av).split("-")
        if len(names) > 1 and len(hv_parts) == len(names) == len(av_parts):
            pairs = zip(names, hv_parts, av_parts)
        else:
            pairs = [(name, hv, av)]
        for n, h, a in pairs:
            h, a = num(h), num(a)
            if h is not None and a is not None:
                stat = re.sub(r"(?<!^)(?=[A-Z])", "_", n).lower()[:32]
                rows.append({"match_id": match_id, "source_id": SOURCE, "stat": stat, "period": 0,
                             "home_value": h, "away_value": a})
    return rows


class Refs:
    """ESPN team ids are per league (1 = Atlanta Hawks in the NBA, Boston Bruins in the NHL): key 'nba|1'."""

    def __init__(self, s, sport_id: int, gender: str, code: str) -> None:
        self.s = s
        self.code = code
        self.sport_id = sport_id
        self.own = {e: i for e, i in s.execute(select(SourceEntity.external_id, SourceEntity.entity_id).where(
            SourceEntity.source_id == SOURCE, SourceEntity.entity_type == "team"))}
        n = dict(s.execute(text("""
            SELECT tid, count(*) FROM (SELECT home_team_id tid FROM matches WHERE sport_id = :sp
                                       UNION ALL SELECT away_team_id FROM matches WHERE sport_id = :sp) x
            GROUP BY tid"""), {"sp": sport_id}).all())
        self.by_name: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for tid, name in s.execute(text("""SELECT t.id, v.n FROM teams t LEFT JOIN source_entities se
              ON se.entity_type = 'team' AND se.entity_id = t.id, LATERAL (VALUES (t.name), (se.external_name)) v(n)
            WHERE v.n IS NOT NULL AND t.sport_id = :sp AND t.gender = :g AND t.age_group IS NULL"""),
                                    {"sp": sport_id, "g": gender}):
            self.by_name[norm_name(name)].append((tid, n.get(tid, 0)))
        self.gender = gender

    def key(self, team: dict) -> str:
        return f"{self.code}|{team['id']}"

    def map(self, team: dict, tid: int, method: str) -> int:
        self.s.add(SourceEntity(source_id=SOURCE, entity_type="team", external_id=self.key(team),
                                external_name=team["name"], entity_id=tid, method=method))
        self.own[self.key(team)] = tid
        return tid

    def team(self, team: dict, country: str | None) -> int:
        if self.key(team) in self.own:
            return self.own[self.key(team)]
        for n in (team["name"], team.get("short"), team.get("location")):
            cands = {}
            for tid, cnt in self.by_name.get(norm_name(n), []):
                cands[tid] = cnt
            ranked = sorted(cands.items(), key=lambda x: -x[1])
            if ranked and (len(ranked) == 1 or ranked[0][1] >= 3 * max(ranked[1][1], 1)):
                return self.map(team, ranked[0][0], "name")
        t = Team(sport_id=self.sport_id, name=team["name"][:160], kind="club", gender=self.gender,
                 country=country)
        self.s.add(t)
        self.s.flush()
        self.by_name[norm_name(team["name"])].append((t.id, 0))
        return self.map(team, t.id, "provider")


def league_id(s, code: str) -> int:
    _, sport_id, (country, name), gender, fd_code = LEAGUES[code]
    lid = s.scalar(select(SourceEntity.entity_id).where(SourceEntity.source_id == SOURCE,
                                                        SourceEntity.entity_type == "league",
                                                        SourceEntity.external_id == code))
    if lid:
        return lid
    if fd_code:  # the football-data league of the same competition
        lid = s.scalar(select(SourceEntity.entity_id).where(SourceEntity.source_id == 1,
                                                            SourceEntity.entity_type == "league",
                                                            SourceEntity.external_id == fd_code))
        if lid:
            s.add(SourceEntity(source_id=SOURCE, entity_type="league", external_id=code, external_name=name,
                               entity_id=lid, method="linked"))
            return lid
    lid = s.scalar(select(League.id).where(League.sport_id == sport_id, League.name == name, League.gender == gender,
                                           League.age_group.is_(None), League.country.is_not_distinct_from(country))
                   .order_by(League.id).limit(1))
    if not lid:
        lg = League(sport_id=sport_id, country=country, name=name, gender=gender,
                    kind="international" if country is None else "league")
        s.add(lg)
        s.flush()
        lid = lg.id
    s.add(SourceEntity(source_id=SOURCE, entity_type="league", external_id=code, external_name=name, entity_id=lid,
                       method="name"))
    return lid


def result(code: str, home: dict, away: dict) -> dict:
    hs, as_ = int(float(home["score"])), int(float(away["score"]))
    per = [[int(h), int(a)] for h, a in zip(home["periods"] or [], away["periods"] or [])
           if h is not None and a is not None]
    extra = {}
    if code == "nhl" and len(per) > 3:
        extra = {"regulation": [sum(p[0] for p in per[:3]), sum(p[1] for p in per[:3])], "overtime": True}
        if len(per) > 4:  # the 5th line is the shootout (its winner got +1 in the final score)
            extra["shootout"] = per[4]
    elif code in ("nba", "wnba") and len(per) > 4:
        extra = {"overtime": len(per) - 4}
    return {"home_score": hs, "away_score": as_, "periods": per or None, "result_extra": extra or None}


def normalize(code: str, keys: list[str]) -> dict:
    _, sport_id, (country, _), gender, _ = LEAGUES[code]
    cnt = defaultdict(int)
    with session() as s:
        refs = Refs(s, sport_id, gender, code)
        lid = league_id(s, code)
        done = set(s.scalars(select(SourceEntity.external_id).where(SourceEntity.source_id == SOURCE,
                                                                    SourceEntity.entity_type == "match")))
    for k in keys:
        with session() as s:
            refs.s = s
            events = s.scalar(select(RawPayload.payload).where(RawPayload.source_id == SOURCE,
                                                               RawPayload.kind == "espn_month",
                                                               RawPayload.external_id == k))["events"]
            events = [e for e in events if e["completed"] and e["id"] not in done and len(e["teams"]) == 2
                      and e["teams"][0].get("score") not in (None, "")]
            if not events:
                continue
            dates = [dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")) for e in events]
            rows = s.execute(text("""
                SELECT m.id, m.home_team_id, m.away_team_id, m.start_time, m.status FROM matches m
                WHERE m.sport_id = :sp AND m.start_time BETWEEN :lo AND :hi AND NOT EXISTS (
                  SELECT 1 FROM source_entities se WHERE se.source_id = :src AND se.entity_type = 'match'
                    AND se.entity_id = m.id)"""), {"sp": sport_id, "lo": min(dates) - dt.timedelta(days=2),
                                                   "hi": max(dates) + dt.timedelta(days=2), "src": SOURCE}).all()
            team_ids = {x for r in rows for x in (r[1], r[2])}
            names = defaultdict(set)
            for tid, n in s.execute(text("""SELECT t.id, t.name FROM teams t WHERE t.id = ANY(:ids) UNION
                SELECT entity_id, external_name FROM source_entities WHERE entity_type = 'team'
                AND entity_id = ANY(:ids) AND external_name IS NOT NULL"""), {"ids": list(team_ids)}):
                names[tid].add(words(n))
            by_day = defaultdict(list)
            for r in rows:
                by_day[r[3].astimezone(dt.timezone.utc).date()].append(r)
            for e, start in zip(events, dates):
                home = next((t for t in e["teams"] if t["side"] == "home"), e["teams"][0])
                away = next(t for t in e["teams"] if t is not home)
                near = [r for d in (-1, 0, 1) for r in by_day.get(start.date() + dt.timedelta(days=d), [])]
                hid, aid = refs.own.get(refs.key(home)), refs.own.get(refs.key(away))
                hit = None
                if hid and aid:
                    hit = next((r for r in near if (r[1], r[2]) == (hid, aid)), None)
                if not hit:  # both sides with similar names: maps the ESPN teams by vote
                    hw, aw = {words(home["name"]), words(home.get("short") or "")}, \
                             {words(away["name"]), words(away.get("short") or "")}
                    scored = []
                    for r in near:
                        sh = 3 if r[1] == hid else similar(hw, names[r[1]])
                        sa = 3 if r[2] == aid else similar(aw, names[r[2]])
                        if sh and sa and (not hid or r[1] == hid) and (not aid or r[2] == aid):
                            scored.append((sh + sa, r))
                    scored.sort(key=lambda x: -x[0])
                    if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]) and scored[0][0] >= 4:
                        hit = scored[0][1]
                        hid = hid or refs.map(home, hit[1], "vote")
                        aid = aid or refs.map(away, hit[2], "vote")
                res = result(code, home, away)
                if hit:
                    mid = hit[0]
                    if hit[4] != "finished":
                        s.execute(text("UPDATE matches SET status = 'finished', home_score = :h, away_score = :a, "
                                       "periods = CAST(:p AS jsonb), result_extra = CAST(:x AS jsonb) WHERE id = :id"),
                                  {"h": res["home_score"], "a": res["away_score"], "id": mid,
                                   "p": json.dumps(res["periods"]) if res["periods"] else None,
                                   "x": json.dumps(res["result_extra"]) if res["result_extra"] else None})
                        cnt["filled"] += 1
                    cnt["linked"] += 1
                else:
                    hid = hid or refs.team(home, country)
                    aid = aid or refs.team(away, country)
                    mid = s.scalar(insert(Match).values(
                        sport_id=sport_id, league_id=lid, stage=(e.get("note") or None) and e["note"][:64],
                        start_time=start, home_team_id=hid, away_team_id=aid, status="finished",
                        **res).returning(Match.id))
                    cnt["new"] += 1
                s.add(SourceEntity(source_id=SOURCE, entity_type="match", external_id=e["id"],
                                   external_name=f"{home['name']} - {away['name']}", entity_id=mid,
                                   method="linked" if hit else "provider"))
                done.add(e["id"])
                st = stat_rows(mid, home, away)
                if st:
                    s.execute(pg_insert(MatchStatistic).on_conflict_do_nothing(), st)
                cnt["stats"] += len(st)
    return dict(cnt)


def minutes(stats: dict) -> float | None:
    """Minutes played: basketball 'MIN' ('34'), hockey 'TOI' ('18:32'); football has none in the summary."""
    v = stats.get("MIN") or stats.get("TOI")
    if not v or v in ("--", "-"):
        return None
    try:
        if ":" in str(v):
            mm, ss = str(v).split(":")[:2]
            return round(int(mm) + int(ss) / 60, 2)
        return float(v)
    except ValueError:
        return None


def normalize_players(code: str, keys: list[str]) -> dict:
    """Lineups / player box scores of the stored games -> players, match_players (+ football key events ->
    match_events), for the matches that have none from ESPN yet."""
    sport_id = LEAGUES[code][1]
    cnt = defaultdict(int)
    for k in keys:
        with session() as s:
            payload = s.scalar(select(RawPayload.payload).where(RawPayload.source_id == SOURCE,
                                                                RawPayload.kind == "espn_month",
                                                                RawPayload.external_id == k))
            events = [e for e in (payload or {}).get("events", []) if any(t.get("players") for t in e["teams"])]
            if not events:
                continue
            match_of = dict(s.execute(select(SourceEntity.external_id, SourceEntity.entity_id).where(
                SourceEntity.source_id == SOURCE, SourceEntity.entity_type == "match",
                SourceEntity.external_id.in_([e["id"] for e in events]))).all())
            have = set(s.scalars(text("SELECT DISTINCT match_id FROM match_players WHERE source_id = :src "
                                      "AND match_id = ANY(:ids)"), {"src": SOURCE, "ids": list(match_of.values())}))
            todo = [e for e in events if e["id"] in match_of and match_of[e["id"]] not in have]
            if not todo:
                continue
            keys_ = {f"{sport_id}|{p['id']}" for e in todo for t in e["teams"] for p in t.get("players") or []}
            pmap = dict(s.execute(select(SourceEntity.external_id, SourceEntity.entity_id).where(
                SourceEntity.source_id == SOURCE, SourceEntity.entity_type == "player",
                SourceEntity.external_id.in_(list(keys_)))).all())
            names = {f"{sport_id}|{p['id']}": p["name"] for e in todo for t in e["teams"]
                     for p in t.get("players") or []}
            new_keys = [x for x in keys_ if x not in pmap]
            if new_keys:
                ids = s.scalars(insert(Player).returning(Player.id, sort_by_parameter_order=True),
                                [{"sport_id": sport_id, "name": (names[x] or "?")[:160]} for x in new_keys]).all()
                s.execute(insert(SourceEntity), [{"source_id": SOURCE, "entity_type": "player", "external_id": x,
                                                  "external_name": (names[x] or "")[:255], "entity_id": i,
                                                  "method": "provider"} for x, i in zip(new_keys, ids)])
                pmap.update(zip(new_keys, ids))
                cnt["players_new"] += len(new_keys)
            rows, evs = [], []
            for e in todo:
                mid = match_of[e["id"]]
                home_t, away_t = s.execute(text("SELECT home_team_id, away_team_id FROM matches WHERE id = :m"),
                                           {"m": mid}).one()
                side_of = {}
                for t in e["teams"]:
                    side = "h" if t["side"] == "home" else "a"
                    side_of[str(t["id"])] = side
                    for p in t.get("players") or []:
                        st = p.get("stats") or {}
                        rows.append({"match_id": mid, "source_id": SOURCE, "side": side,
                                     "team_id": home_t if side == "h" else away_t,
                                     "player_id": pmap[f"{sport_id}|{p['id']}"], "starter": p.get("starter"),
                                     "position": (p.get("pos") or None) and p["pos"][:8],
                                     "minutes": minutes(st),
                                     "dnp_reason": (str(p.get("reason") or "did not play")[:64]
                                                    if p.get("dnp") else None),
                                     "stats": {**st, **({"sub_in": True} if p.get("sub_in") else {}),
                                               **({"sub_out": True} if p.get("sub_out") else {})} or None})
                for ev in e.get("events") or []:
                    who = ev["players"][0] if ev.get("players") else None
                    evs.append({"match_id": mid, "source_id": SOURCE, "type": ev["type"], "minute": ev["minute"],
                                "side": side_of.get(ev.get("team")),
                                "player_id": pmap.get(f"{sport_id}|{who}") if who else None,
                                "detail": {"players": [pmap.get(f"{sport_id}|{x}") for x in ev["players"]]}
                                if len(ev.get("players") or []) > 1 else None})
            for c in range(0, len(rows), 5000):
                s.execute(pg_insert(MatchPlayer).on_conflict_do_nothing(), rows[c:c + 5000])
            if evs:
                s.execute(insert(MatchEvent), evs)
            cnt["matches"] += len(todo)
            cnt["player_rows"] += len(rows)
            cnt["events"] += len(evs)
    return dict(cnt)


def refetch_details(http: HttpClient, code: str, keys: list[str]) -> int:
    """History stored before lineups were kept: the summary again for every finished game without players."""
    path = LEAGUES[code][0]
    n = 0
    for k in keys:
        with session() as s:
            payload = s.scalar(select(RawPayload.payload).where(RawPayload.source_id == SOURCE,
                                                                RawPayload.kind == "espn_month",
                                                                RawPayload.external_id == k))
        if not payload:
            continue
        changed = False
        for e in payload["events"]:
            if not e["completed"] or any(t.get("players") for t in e["teams"]):
                continue
            d = summary_of(http, path, e["id"])
            for t in e["teams"]:
                t["players"] = d["players"].get(str(t["id"]))
                t["stats"] = t.get("stats") or d["stats"].get(str(t["id"]))
            e["events"] = d["events"]
            changed, n = True, n + 1
        if changed:
            with session() as s:
                s.execute(delete(RawPayload).where(RawPayload.source_id == SOURCE, RawPayload.kind == "espn_month",
                                                   RawPayload.external_id == k))
                s.execute(insert(RawPayload).values(source_id=SOURCE, kind="espn_month", external_id=k,
                                                    payload=payload))
            print(f"  {k}: details re-fetched", flush=True)
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2024-11-01")
    ap.add_argument("--leagues", default=",".join(LEAGUES))
    ap.add_argument("--refresh-days", type=int, default=3, help="re-fetch the month holding the last N days")
    ap.add_argument("--recent", type=int, metavar="N", help="hourly: only today and the N-1 days before (UTC)")
    ap.add_argument("--details", action="store_true",
                    help="re-fetch the summaries of stored games that have no lineups / player box scores yet")
    args = ap.parse_args()
    since = dt.date.fromisoformat(args.since)
    yesterday = dt.date.today() - dt.timedelta(days=1)
    http = HttpClient(min_interval=0.3, retries=3, timeout=15)
    t0 = time.time()
    failed = False
    for code in [c.strip() for c in args.leagues.split(",") if c.strip()]:
        if args.recent:
            today = dt.datetime.now(dt.timezone.utc).date()
            try:
                keys = update_days(http, code, [today - dt.timedelta(days=i) for i in range(args.recent - 1, -1, -1)])
            except httpx.HTTPError as e:  # ESPN unreachable now and then: skip the rest, the next hour retries
                print(f"{code}: ESPN not reachable ({e!r:.100}), the other leagues wait for the next run", flush=True)
                failed = True
                break
            print(f"{code}: {normalize(code, keys)} players {normalize_players(code, keys)}", flush=True)
            continue
        with session() as s:
            have = set(s.scalars(select(RawPayload.external_id).where(RawPayload.source_id == SOURCE,
                                                                      RawPayload.kind == "espn_month",
                                                                      RawPayload.external_id.like(f"{code}|%"))))
        recent = (yesterday - dt.timedelta(days=args.refresh_days)).strftime("%Y-%m")
        keys = []
        for lo, hi in months(since, yesterday):
            key = f"{code}|{lo:%Y-%m}"
            keys.append(key)
            if key in have and lo.strftime("%Y-%m") < recent:
                continue
            events = fetch(http, code, lo, hi)
            with session() as s:
                s.execute(delete(RawPayload).where(RawPayload.source_id == SOURCE, RawPayload.kind == "espn_month",
                                                   RawPayload.external_id == key))
                s.execute(insert(RawPayload).values(source_id=SOURCE, kind="espn_month", external_id=key,
                                                    payload={"events": events}))
            print(f"  {key}: {len(events)} games ({sum(1 for e in events if e['completed'])} finished)  "
                  f"{(time.time() - t0) / 60:.1f} min", flush=True)
        if args.details:
            print(f"{code}: {refetch_details(http, code, keys)} summaries re-fetched", flush=True)
        print(f"{code}: {normalize(code, keys)} players {normalize_players(code, keys)}", flush=True)
    print(f"done in {(time.time() - t0) / 60:.1f} min")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
