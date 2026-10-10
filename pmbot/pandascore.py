"""PandaScore REST (free tier): match list with per-map begin_at / end_at. Match id = Polymarket gameId.

Free tier: list endpoints (/csgo/matches?filter[id]=...), 1000 requests/hour. Match/game detail, rounds and
live WebSockets return 403 / 'Unsufficient permission'.
"""

import datetime as dt
import os
import sqlite3
import time

import httpx

API = "https://api.pandascore.co"
PAGE = 100


class PandaScore:
    def __init__(self, token: str | None = None) -> None:
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {token or os.environ['PANDASCORE_ACCESS_TOKEN']}"}, timeout=30)

    def matches(self, ids: list[int]) -> list[dict]:
        for attempt in range(5):
            r = self._client.get(f"{API}/csgo/matches", params={
                "filter[id]": ",".join(map(str, ids)), "per_page": PAGE})
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
        statuses = {m["id"]: m.get("status") for m in matches}
        conn.executemany("INSERT OR REPLACE INTO ps_matches (match_id, status, fetched_at) VALUES (?,?,?)",
                         [(i, statuses.get(i, "missing"), now) for i in requested])
