"""cybersport.ru unofficial JSON API: CS2 / Dota 2 match list, map scores, lineups and CS2 round-by-round data.

- GET /api/matches?filter[date]=YYYY-MM-DD&filter[disciplineIds]=22  -> matches of the day (Moscow date; Dota 2 = 21)
- GET /api/matches/{id}                                              -> games (maps) with map names and scores,
      lineup that played (Dota: player -> dota2_player.externalId = Steam account id), bracket / round, prize pool;
      Dota game externalApiId = Steam match id (OpenDota)
- GET /api/v1/match-stats/csgo?filter[internalId]={id}               -> rounds and player stats per game; 404 if no stats
- GET /api/teams/{id}                                                -> roster history, player nationality
Tournament (`stage`) carries `location` ("Город, Страна") and since/till dates.
`externalId` in match-stats looks like the PandaScore match id, same as Polymarket `gameId`.
"""

import datetime as dt
import json
import sqlite3

from pmbot.http import HttpClient

API = "https://www.cybersport.ru/api"
CS2_DISCIPLINE_ID = 22
DISCIPLINES = {"cs2": CS2_DISCIPLINE_ID, "dota2": 21}
_ROUND_RELS = ("swissRound", "flatRound", "groupTable", "singleCell", "doubleCell", "bracketMatch")


def _index(doc: dict) -> dict:
    return {(x["type"], x["id"]): x for x in doc.get("included", [])}


def _ref(obj: dict, rel: str) -> dict | None:
    return ((obj.get("relationships") or {}).get(rel) or {}).get("data")


def _ymd(d: dict | None) -> str | None:
    if not d or not d.get("year"):
        return None
    return dt.date(d["year"], d.get("month") or 1, d.get("day") or 1).isoformat()


def list_matches(http: HttpClient, date: dt.date, discipline: int = CS2_DISCIPLINE_ID) -> list[dict]:
    """Matches of one day with team names and tournament resolved from `included`."""
    result, offset = [], 0
    while True:
        doc = http.get_json(f"{API}/matches", {
            "filter[date]": date.isoformat(),
            "filter[disciplineIds]": discipline,
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
                    "team_id": team["id"] if team else None,
                    "score": p["attributes"].get("matchScore") if p else None,
                })
            stage_ref = _ref(m, "stage")
            stage = inc.get(("stage", stage_ref["id"])) if stage_ref else None
            sa = stage["attributes"] if stage else {}
            a = m["attributes"]
            result.append({
                "id": m["id"],
                "date": a.get("date"),
                "time": a.get("time"),
                "format": a.get("format"),
                "is_finished": bool(a.get("isFinished")),
                "team1": parts[0]["team"], "score1": parts[0]["score"],
                "team2": parts[1]["team"], "score2": parts[1]["score"],
                "team1_id": parts[0]["team_id"], "team2_id": parts[1]["team_id"],
                "tournament": sa.get("title"),
                "tier": sa.get("tier"),
                "location": sa.get("location"),
                "stage_since": _ymd(sa.get("since")),
                "stage_till": _ymd(sa.get("till")),
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
    return _games(http.get_json(f"{API}/matches/{match_id}"))


def fetch_details(http: HttpClient, match_id: str) -> dict:
    """Maps (Dota: external_id = Steam match id), lineup that played, bracket/round name, prize pool."""
    doc = http.get_json(f"{API}/matches/{match_id}")
    inc = _index(doc)
    rels = doc["data"].get("relationships") or {}
    players = []
    for side in (1, 2):
        part_ref = _ref(doc["data"], f"participant{side}")
        part = inc.get((part_ref["type"], part_ref["id"])) if part_ref else None
        team_ref = _ref(part, "team") if part else None
        for ref in (rels.get(f"participant{side}TeamPlayers") or {}).get("data") or []:
            tp = inc.get((ref["type"], ref["id"]))
            p_ref = _ref(tp, "player") if tp else None
            p = inc.get(("player", p_ref["id"])) if p_ref else None
            if not p:
                continue
            ext_ref = _ref(p, "dotaPlayer")
            ext = inc.get((ext_ref["type"], ext_ref["id"])) if ext_ref else None
            players.append({
                "side": side, "team_id": team_ref["id"] if team_ref else None, "player_id": p["id"],
                "nickname": p["attributes"].get("nickname"), "country_code": p["attributes"].get("countryCode"),
                "steam_id": ext["attributes"].get("externalId") if ext else None,
                "status": tp["attributes"].get("status"), "position": tp["attributes"].get("position"),
            })
    stage_ref, bracket_ref = _ref(doc["data"], "stage"), _ref(doc["data"], "bracket")
    stage = inc.get((stage_ref["type"], stage_ref["id"])) if stage_ref else None
    bracket = inc.get((bracket_ref["type"], bracket_ref["id"])) if bracket_ref else None
    round_name = None
    for rel in _ROUND_RELS:
        ref = _ref(doc["data"], rel)
        obj = inc.get((ref["type"], ref["id"])) if ref else None
        if obj:
            round_name = obj["attributes"].get("name") or obj["attributes"].get("title")
            break
    return {
        "games": _games(doc), "players": players,
        "prize_pool": (stage or {}).get("attributes", {}).get("prizePool"),
        "bracket": (bracket or {}).get("attributes", {}).get("name"), "round_name": round_name,
    }


def save_details(conn: sqlite3.Connection, match_id: str, details: dict) -> None:
    with conn:
        conn.execute("UPDATE cs_matches SET prize_pool=?, bracket=?, round_name=? WHERE id=?",
                     (details["prize_pool"], details["bracket"], details["round_name"], match_id))
        conn.execute("DELETE FROM cs_match_players WHERE match_id=?", (match_id,))
        conn.executemany(
            """INSERT OR REPLACE INTO cs_match_players (match_id, side, team_id, player_id, nickname, country_code,
                   steam_id, status, position) VALUES (?,?,?,?,?,?,?,?,?)""",
            [(match_id, p["side"], p["team_id"], p["player_id"], p["nickname"], p["country_code"], p["steam_id"],
              p["status"], p["position"]) for p in details["players"]])


def _games(doc: dict) -> list[dict]:
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


def _save_meta(conn: sqlite3.Connection, match: dict) -> None:
    conn.execute(
        "UPDATE cs_matches SET team1_id=?, team2_id=?, location=?, stage_since=?, stage_till=? WHERE id=?",
        (match["team1_id"], match["team2_id"], match["location"], match["stage_since"], match["stage_till"], match["id"]),
    )


def save_meta(conn: sqlite3.Connection, match: dict) -> None:
    """Team ids and tournament location/dates for an already stored match (no extra requests)."""
    with conn:
        _save_meta(conn, match)


def fetch_team(http: HttpClient, team_id: str) -> dict:
    """Team roster history: players with nationality and since/till dates."""
    doc = http.get_json(f"{API}/teams/{team_id}")
    inc = _index(doc)
    countries = {x["attributes"]["code"]: x["attributes"].get("title")
                 for x in doc.get("included", []) if x["type"] == "country" and x["attributes"].get("code")}
    players = []
    for x in doc.get("included", []):
        if x["type"] != "team_player" or x["attributes"].get("isTotal"):
            continue
        p_ref = _ref(x, "player")
        p = inc.get(("player", p_ref["id"])) if p_ref else None
        players.append({
            "player_id": p_ref["id"] if p_ref else None,
            "nickname": p["attributes"].get("nickname") if p else None,
            "country_code": p["attributes"].get("countryCode") if p else None,
            "status": x["attributes"].get("status"),
            "since": x["attributes"].get("since") or "",
            "till": x["attributes"].get("till"),
        })
    return {"id": team_id, "title": doc["data"]["attributes"].get("title"), "players": players, "countries": countries}


def save_team(conn: sqlite3.Connection, team: dict) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO cs_teams (id, title, fetched_at) VALUES (?,?,?)",
                     (team["id"], team["title"], dt.datetime.now(dt.timezone.utc).isoformat()))
        conn.execute("DELETE FROM cs_team_players WHERE team_id=?", (team["id"],))
        conn.executemany(
            """INSERT OR REPLACE INTO cs_team_players (team_id, player_id, nickname, country_code, status, since, till)
               VALUES (?,?,?,?,?,?,?)""",
            [(team["id"], p["player_id"], p["nickname"], p["country_code"], p["status"], p["since"], p["till"])
             for p in team["players"] if p["player_id"]],
        )
        conn.executemany("INSERT OR REPLACE INTO cs_countries (code, title) VALUES (?,?)", team["countries"].items())


def player_stats(stats: dict) -> list[tuple]:
    """(game_id, player_id, nickname, kills, deaths, assists, adr, kast, rating) per player per map (totals skipped)."""
    inc = _index(stats)
    out = []
    for g in stats.get("included", []):
        if g["type"] != "csgo_match_game" or g["attributes"].get("isTotal") or not g["attributes"].get("internalId"):
            continue
        for ref in (g.get("relationships") or {}).get("participants", {}).get("data") or []:
            p = inc.get(("csgo_match_game_participant", ref["id"]))
            pl_ref = _ref(p, "player") if p else None
            pl = inc.get(("csgo_player", pl_ref["id"])) if pl_ref else None
            if not pl:
                continue
            a, pa = p["attributes"], pl["attributes"]
            out.append((str(g["attributes"]["internalId"]), str(pa.get("internalId")),
                        (pa.get("externalName") or "").split(" (")[0], a.get("kills"), a.get("deaths"),
                        a.get("assists"), a.get("adr"), a.get("kast"), a.get("rating")))
    return out


def save_player_stats(conn: sqlite3.Connection, match_id: str, stats: dict | None) -> int:
    rows = player_stats(stats) if stats else []
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO cs_player_stats (game_id, player_id, nickname, kills, deaths, assists, adr, kast, rating)
               VALUES (?,?,?,?,?,?,?,?,?)""", rows)
        conn.execute("INSERT OR REPLACE INTO cs_player_stats_status (match_id, players, fetched_at) VALUES (?,?,?)",
                     (match_id, len(rows), dt.datetime.now(dt.timezone.utc).isoformat()))
    return len(rows)


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
        _save_meta(conn, match)
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
