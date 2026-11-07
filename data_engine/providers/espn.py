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
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
from collections import defaultdict

from sqlalchemy import delete, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db import session
from core.models import League, Match, MatchStatistic, RawPayload, SourceEntity, Team
from data_engine.normalization.polymarket_links import similar, words
from data_engine.providers.polymarket_prices import norm_name
from pmbot.http import HttpClient

SOURCE = 9  # espn
API = "https://site.api.espn.com/apis/site/v2/sports"
# code -> (espn path, sport id, our league (country, name), gender)
LEAGUES = {
    "nba": ("basketball/nba", 3, ("USA", "NBA"), "m"),
    "wnba": ("basketball/wnba", 3, ("USA", "WNBA"), "f"),
    "nhl": ("hockey/nhl", 5, ("USA", "NHL"), "m"),
    "ucl": ("soccer/uefa.champions", 1, (None, "UEFA Champions League"), "m"),
    "uel": ("soccer/uefa.europa", 1, (None, "UEFA Europa League"), "m"),
    "uecl": ("soccer/uefa.europa.conf", 1, (None, "UEFA Conference League"), "m"),
    "unl": ("soccer/uefa.nations", 1, (None, "UEFA Nations League"), "m"),
    "efl": ("soccer/eng.league_cup", 1, ("England", "EFL Cup"), "m"),
}


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


def team_name(name: str | None) -> str | None:
    """'UEFA Nations League: Netherlands' / 'UCL: Real Madrid' (ESPN's placeholder names) -> 'Netherlands'."""
    return re.sub(r"^(UEFA [A-Za-z ]+League|UCL|UEL|UECL):\s*", "", name) if name else name


def compact(e: dict, stats: dict) -> dict:
    c = e["competitions"][0]
    return {"id": e["id"], "date": e["date"], "status": c["status"]["type"].get("name"),
            "completed": c["status"]["type"].get("completed"), "neutral": c.get("neutralSite"),
            "season": (e.get("season") or {}).get("year"), "season_type": (e.get("season") or {}).get("type"),
            "note": (c.get("notes") or [{}])[0].get("headline") if c.get("notes") else None,
            "teams": [{"id": t["team"]["id"], "name": team_name(t["team"].get("displayName")),
                       "short": t["team"].get("shortDisplayName"), "abbr": t["team"].get("abbreviation"),
                       "location": t["team"].get("location"), "side": t.get("homeAway"), "score": t.get("score"),
                       "winner": t.get("winner"), "shootout": t.get("shootoutScore"),
                       "periods": [x.get("value") for x in t.get("linescores") or []],
                       "stats": stats.get(str(t["team"]["id"]))} for t in c["competitors"]]}


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
        stats = {}
        if c["status"]["type"].get("completed"):
            try:
                stats = box(http.get_json(f"{API}/{path}/summary", {"event": e["id"]}))
            except Exception:  # box score missing: the result is still useful
                stats = {}
        out.append(compact(e, stats))
    return out


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
    _, sport_id, (country, name), gender = LEAGUES[code]
    lid = s.scalar(select(SourceEntity.entity_id).where(SourceEntity.source_id == SOURCE,
                                                        SourceEntity.entity_type == "league",
                                                        SourceEntity.external_id == code))
    if lid:
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
    _, sport_id, (country, _), gender = LEAGUES[code]
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2024-11-01")
    ap.add_argument("--leagues", default=",".join(LEAGUES))
    ap.add_argument("--refresh-days", type=int, default=3, help="re-fetch the month holding the last N days")
    args = ap.parse_args()
    since = dt.date.fromisoformat(args.since)
    yesterday = dt.date.today() - dt.timedelta(days=1)
    http = HttpClient(min_interval=0.3, retries=5, timeout=30)
    t0 = time.time()
    for code in [c.strip() for c in args.leagues.split(",") if c.strip()]:
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
        print(f"{code}: {normalize(code, keys)}", flush=True)
    print(f"done in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
