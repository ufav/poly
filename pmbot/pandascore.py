"""PandaScore REST (free tier): matches with per-map begin_at / end_at, tournament, teams. Match id = Polymarket gameId.

Free tier: list endpoints (/{game}/matches?filter[id]=..., /{game}/matches/past, /{game}/teams), 1000 requests/hour.
Match/game detail, rounds, draft and live WebSockets return 403/404 / 'Unsufficient permission'.
Game slugs: csgo (CS2), dota2.
"""

import datetime as dt
import json
import os
import sqlite3
import time

import httpx

API = "https://api.pandascore.co"
PAGE = 100
GAMES = {"cs2": "csgo", "dota2": "dota2"}


class PandaScore:
    def __init__(self, token: str | None = None, game: str = "csgo") -> None:
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {token or os.environ['PANDASCORE_ACCESS_TOKEN']}"}, timeout=30)
        self.game = game

    def get(self, path: str, params: dict) -> list[dict]:
        for attempt in range(5):
            r = self._client.get(f"{API}/{self.game}/{path}", params=params)
            if r.status_code == 429:
                time.sleep(60)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return []

    def matches(self, ids: list[int]) -> list[dict]:
        return self.get("matches", {"filter[id]": ",".join(map(str, ids)), "per_page": PAGE})

    def past_matches(self, since: dt.datetime, until: dt.datetime, page: int) -> list[dict]:
        rng = f"{since:%Y-%m-%dT%H:%M:%SZ},{until:%Y-%m-%dT%H:%M:%SZ}"
        return self.get("matches/past", {"range[begin_at]": rng, "sort": "begin_at", "per_page": PAGE, "page": page})

    def teams(self, ids: list[int]) -> list[dict]:
        return self.get("teams", {"filter[id]": ",".join(map(str, ids)), "per_page": PAGE})

    def close(self) -> None:
        self._client.close()


def save_matches(conn: sqlite3.Connection, requested: list[int], matches: list[dict]) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with conn:
        for m in matches:
            conn.executemany(
                """INSERT OR REPLACE INTO ps_games (match_id, position, game_id, status, begin_at, end_at, length, winner_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [(m["id"], g.get("position"), g.get("id"), g.get("status"), g.get("begin_at"), g.get("end_at"),
                  g.get("length"), (g.get("winner") or {}).get("id")) for g in m.get("games") or []])
            _save_info(conn, m, now)
        statuses = {m["id"]: m.get("status") for m in matches}
        conn.executemany("INSERT OR REPLACE INTO ps_matches (match_id, status, fetched_at) VALUES (?,?,?)",
                         [(i, statuses.get(i, "missing"), now) for i in requested])


def _save_info(conn: sqlite3.Connection, m: dict, now: str) -> None:
    teams = [o.get("opponent") or {} for o in m.get("opponents") or []]
    score = {r.get("team_id"): r.get("score") for r in m.get("results") or []}
    t1, t2 = (teams + [{}, {}])[:2]
    conn.execute(
        """INSERT OR REPLACE INTO ps_match_info (match_id, name, status, match_type, number_of_games, scheduled_at,
               original_scheduled_at, rescheduled, begin_at, end_at, forfeit, winner_id, team1_id, team2_id, score1,
               score2, tournament_id, serie_id, league_id, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (m["id"], m.get("name"), m.get("status"), m.get("match_type"), m.get("number_of_games"), m.get("scheduled_at"),
         m.get("original_scheduled_at"), int(bool(m.get("rescheduled"))), m.get("begin_at"), m.get("end_at"),
         int(bool(m.get("forfeit"))), m.get("winner_id"), t1.get("id"), t2.get("id"), score.get(t1.get("id")),
         score.get(t2.get("id")), m.get("tournament_id"), m.get("serie_id"), m.get("league_id"),
         json.dumps({k: v for k, v in m.items() if k != "streams_list"}, ensure_ascii=False, separators=(",", ":"))))
    t, s, lg = m.get("tournament") or {}, m.get("serie") or {}, m.get("league") or {}
    if t.get("id"):
        conn.execute(
            """INSERT OR REPLACE INTO ps_tournaments (id, name, type, country, region, tier, prizepool, begin_at, end_at,
                   has_bracket, serie_id, serie_name, serie_year, league_id, league_name)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t["id"], t.get("name"), t.get("type"), t.get("country"), t.get("region"), t.get("tier"), t.get("prizepool"),
             t.get("begin_at"), t.get("end_at"), int(bool(t.get("has_bracket"))), s.get("id"), s.get("full_name"),
             s.get("year"), lg.get("id"), lg.get("name")))
    for team in teams:
        if team.get("id"):
            conn.execute("""INSERT INTO ps_teams (id, name, acronym, slug, location) VALUES (?,?,?,?,?)
                            ON CONFLICT(id) DO UPDATE SET name=excluded.name, acronym=excluded.acronym,
                                slug=excluded.slug, location=excluded.location""",
                         (team["id"], team.get("name"), team.get("acronym"), team.get("slug"), team.get("location")))


def save_teams(conn: sqlite3.Connection, teams: list[dict]) -> int:
    """Team location and current roster (nationality, role)."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    n = 0
    with conn:
        for team in teams:
            conn.execute("INSERT OR REPLACE INTO ps_teams (id, name, acronym, slug, location, fetched_at) VALUES (?,?,?,?,?,?)",
                         (team["id"], team.get("name"), team.get("acronym"), team.get("slug"), team.get("location"), now))
            for p in team.get("players") or []:
                conn.execute(
                    """INSERT OR REPLACE INTO ps_players (id, name, first_name, last_name, nationality, role, age,
                           birthday, active, team_id) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (p["id"], p.get("name"), p.get("first_name"), p.get("last_name"), p.get("nationality"),
                     p.get("role"), p.get("age"), p.get("birthday"), int(bool(p.get("active"))), team["id"]))
                n += 1
    return n
