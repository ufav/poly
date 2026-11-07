"""ESPN injury reports (site.api.espn.com .../{league}/injuries) of NBA, WNBA, NHL -> injuries, append-only: a row
when a player's status or comment changed since his last stored row, and an 'Active' row when a player dropped off
the report (he is back). The report has no history at the source, so it is collected from now on (hourly, in
workers/pipeline.py); for past games the lineups / did-not-play reasons of match_players stand in.
Football: ESPN has no injury report (empty) - news via the LLM step later.

  python -m data_engine.providers.espn_injuries
"""

from __future__ import annotations

import datetime as dt
import sys

import httpx
from sqlalchemy import insert, select, text

from core.db import session
from core.models import Injury, Player, SourceEntity
from data_engine.providers.espn import API, LEAGUES, SOURCE
from pmbot.http import HttpClient

CODES = ("nba", "wnba", "nhl")


def player_id(s, sport_id: int, athlete: dict, cache: dict) -> int:
    key = f"{sport_id}|{athlete.get('id') or athlete.get('uid', '').split('~a:')[-1]}"
    if key in cache:
        return cache[key]
    pid = s.scalar(select(SourceEntity.entity_id).where(SourceEntity.source_id == SOURCE,
                                                        SourceEntity.entity_type == "player",
                                                        SourceEntity.external_id == key))
    if not pid:
        p = Player(sport_id=sport_id, name=(athlete.get("displayName") or "?")[:160])
        s.add(p)
        s.flush()
        pid = p.id
        s.add(SourceEntity(source_id=SOURCE, entity_type="player", external_id=key,
                           external_name=(athlete.get("displayName") or "")[:255], entity_id=pid, method="provider"))
    cache[key] = pid
    return pid


def collect(http: HttpClient, code: str) -> dict:
    path, sport_id = LEAGUES[code][0], LEAGUES[code][1]
    data = http.get_json(f"{API}/{path}/injuries")
    cnt = {"reported": 0, "changed": 0, "back": 0}
    with session() as s:
        teams = dict(s.execute(select(SourceEntity.external_id, SourceEntity.entity_id).where(
            SourceEntity.source_id == SOURCE, SourceEntity.entity_type == "team",
            SourceEntity.external_id.like(f"{code}|%"))).all())
        last = {pid: (status, comment) for pid, status, comment in s.execute(text("""
            SELECT DISTINCT ON (player_id) player_id, status, comment FROM injuries
            WHERE source_id = :src AND extra->>'league' = :code ORDER BY player_id, fetched_at DESC"""),
            {"src": SOURCE, "code": code})}
        cache: dict = {}
        seen, rows = set(), []
        for t in data.get("injuries") or []:
            team_id = teams.get(f"{code}|{t.get('id')}")
            for inj in t.get("injuries") or []:
                ath = inj.get("athlete") or {}
                if not ath:
                    continue
                pid = player_id(s, sport_id, {**ath, "id": ath.get("id") or inj.get("id")}, cache)
                seen.add(pid)
                cnt["reported"] += 1
                status = (inj.get("status") or "?")[:32]
                comment = inj.get("shortComment") or inj.get("longComment")
                if last.get(pid) == (status, comment):
                    continue
                reported = inj.get("date")
                rows.append({"source_id": SOURCE, "sport_id": sport_id, "team_id": team_id, "player_id": pid,
                             "status": status, "comment": comment,
                             "reported_at": dt.datetime.fromisoformat(reported.replace("Z", "+00:00"))
                             if reported else None,
                             "extra": {"league": code, "long": inj.get("longComment"),
                                       "type": (inj.get("type") or {}).get("name"), "details": inj.get("details")}})
                cnt["changed"] += 1
        for pid, (status, _) in last.items():  # off the report: back in the squad
            if pid not in seen and status != "Active":
                rows.append({"source_id": SOURCE, "sport_id": sport_id, "team_id": None, "player_id": pid,
                             "status": "Active", "comment": "off the injury report", "reported_at": None,
                             "extra": {"league": code}})
                cnt["back"] += 1
        if rows:
            s.execute(insert(Injury), rows)
    return cnt


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    http = HttpClient(min_interval=0.5, retries=3, timeout=15)
    failed = False
    for code in CODES:
        try:
            print(f"{code}: {collect(http, code)}", flush=True)
        except httpx.HTTPError as e:  # ESPN unreachable now and then: the next hour retries
            print(f"{code}: not fetched ({e!r:.100})", flush=True)
            failed = True
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
