"""cybersport.ru unofficial JSON API: CS2 match list, map scores and round-by-round data.

- GET /api/matches?filter[date]=YYYY-MM-DD&filter[disciplineIds]=22  -> matches of the day (Moscow date)
- GET /api/matches/{id}                                              -> games (maps) with map names and scores
- GET /api/v1/match-stats/csgo?filter[internalId]={id}               -> rounds per game; 404 if no stats
`externalId` in match-stats looks like the PandaScore match id, same as Polymarket `gameId`.
"""

import datetime as dt
import json
import sqlite3

from pmbot.http import HttpClient

API = "https://www.cybersport.ru/api"
CS2_DISCIPLINE_ID = 22


def _index(doc: dict) -> dict:
    return {(x["type"], x["id"]): x for x in doc.get("included", [])}


def _ref(obj: dict, rel: str) -> dict | None:
    return ((obj.get("relationships") or {}).get(rel) or {}).get("data")


def list_matches(http: HttpClient, date: dt.date) -> list[dict]:
    """Matches of one day with team names and tournament resolved from `included`."""
    result, offset = [], 0
    while True:
        doc = http.get_json(f"{API}/matches", {
            "filter[date]": date.isoformat(),
            "filter[disciplineIds]": CS2_DISCIPLINE_ID,
            "page[limit]": 100,
            "page[offset]": offset,
        })
        inc = _index(doc)
        for m in doc["data"]:
            parts = []
            for rel in ("participant1", "participant2"):
                p_ref = _ref(m, rel)
                p = inc.get((p_ref["type"], p_ref["id"])) if p_ref else None
                team_ref = _ref(p, "team") if p else None
                team = inc.get(("team", team_ref["id"])) if team_ref else None
                parts.append({
                    "team": team["attributes"].get("title") if team else None,
                    "score": p["attributes"].get("matchScore") if p else None,
                })
            stage_ref = _ref(m, "stage")
            stage = inc.get(("stage", stage_ref["id"])) if stage_ref else None
            a = m["attributes"]
            result.append({
                "id": m["id"],
                "date": a.get("date"),
                "time": a.get("time"),
                "format": a.get("format"),
                "is_finished": bool(a.get("isFinished")),
                "team1": parts[0]["team"], "score1": parts[0]["score"],
                "team2": parts[1]["team"], "score2": parts[1]["score"],
                "tournament": stage["attributes"].get("title") if stage else None,
                "tier": stage["attributes"].get("tier") if stage else None,
                "odds": a.get("odds") or [],
            })
        if len(doc["data"]) < 100:
            return result
        offset += 100


def fetch_stats(http: HttpClient, match_id: str) -> dict | None:
    r = http.get(f"{API}/v1/match-stats/csgo", {"filter[internalId]": match_id})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def fetch_games(http: HttpClient, match_id: str) -> list[dict]:
    """Maps in play order: internal game id, map name, per-map score."""
    doc = http.get_json(f"{API}/matches/{match_id}")
    inc = _index(doc)
    games = []
    for number, ref in enumerate((_ref(doc["data"], "games") or []), 1):
        g = inc.get(("match_game", ref["id"]))
        if not g:
            continue
        map_ref = _ref(g, "map")
        map_obj = inc.get(("discipline_map", map_ref["id"])) if map_ref else None
        a = g["attributes"]
        games.append({
            "id": g["id"],
            "number": number,
            "map_name": map_obj["attributes"].get("name") if map_obj else None,
            "score1": a.get("participant1Score"),
            "score2": a.get("participant2Score"),
            "external_id": a.get("externalApiId"),
        })
    return games


def save_match(conn: sqlite3.Connection, match: dict, stats: dict | None, games: list[dict]) -> None:
    stats_team = {}
    external_id = begin_at = None
    rounds_by_game: dict[str, list[dict]] = {}
    if stats:
        data = stats["data"]
        external_id = int(data["attributes"]["externalId"]) if data["attributes"].get("externalId") else None
        begin_at = data["attributes"].get("beginAt")
        names = {x["id"]: x["attributes"].get("externalName") for x in stats.get("included", []) if x["type"] == "csgo_team"}
        for rel in ("team1", "team2"):
            ref = _ref(data, rel)
            stats_team[rel] = names.get(ref["id"]) if ref else None
        for g in stats.get("included", []):
            if g["type"] == "csgo_match_game" and not g["attributes"].get("isTotal"):
                rounds = [{**r, "ct": names.get(r["ct"], r["ct"]), "terrorists": names.get(r["terrorists"], r["terrorists"]),
                           "winner_team": names.get(r["winner_team"], r["winner_team"])}
                          for r in g["attributes"].get("rounds") or []]
                rounds_by_game[str(g["attributes"].get("internalId"))] = rounds

    with conn:
        conn.execute(
            """INSERT INTO cs_matches (id, date, time, format, is_finished, team1, team2, score1, score2, tournament,
                                       tier, external_id, stats_team1, stats_team2, begin_at, odds, stats_status, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET is_finished=excluded.is_finished, score1=excluded.score1,
                   score2=excluded.score2, external_id=excluded.external_id, stats_team1=excluded.stats_team1,
                   stats_team2=excluded.stats_team2, begin_at=excluded.begin_at, odds=excluded.odds,
                   stats_status=excluded.stats_status, fetched_at=excluded.fetched_at""",
            (match["id"], match["date"], match["time"], match["format"], int(match["is_finished"]), match["team1"],
             match["team2"], match["score1"], match["score2"], match["tournament"], match["tier"], external_id,
             stats_team.get("team1"), stats_team.get("team2"), begin_at, json.dumps(match["odds"], ensure_ascii=False),
             "done" if stats else "none", dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        for g in games:
            rounds = rounds_by_game.get(g["id"], [])
            conn.execute(
                """INSERT OR REPLACE INTO cs_games (id, match_id, number, map_name, score1, score2, external_id, rounds)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (g["id"], match["id"], g["number"], g["map_name"], g["score1"], g["score2"], g["external_id"], len(rounds)),
            )
            conn.executemany(
                """INSERT OR REPLACE INTO cs_rounds (game_id, round, ct_team, t_team, winner_side, winner_team, outcome)
                   VALUES (?,?,?,?,?,?,?)""",
                [(g["id"], r["round"], r["ct"], r["terrorists"], r["winner_side"], r["winner_team"], r.get("outcome"))
                 for r in rounds],
            )
