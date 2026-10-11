"""OpenDota (free, no key): pro Dota 2 games via the SQL explorer, in batches of 100 games per request.

- GET /api/explorer?sql=...   tables `matches` (league games), `player_matches`, `leagues`, `teams`, `team_rating`,
                              `notable_players`, `match_patch`, `heroes`; everything parsed (gold/xp per minute,
                              objectives, teamfights, per-player logs)
- GET /api/constants/items    item id -> name/cost (player item slots are ids, purchase logs use names)
Limits without a key: 60 requests/min, 3000/day (headers x-rate-limit-remaining-*).
Game `match_id` = Steam match id = cybersport `match_game.externalApiId`.
"""

import datetime as dt
import json
import sqlite3
import zlib

from pmbot.http import HttpClient

API = "https://api.opendota.com/api"
BATCH = 100

MATCH_COLUMNS = (
    "match_id", "start_time", "duration", "radiant_win", "leagueid", "series_id", "series_type",
    "radiant_team_id", "dire_team_id", "radiant_team_name", "dire_team_name", "radiant_score", "dire_score",
    "radiant_captain", "dire_captain", "first_blood_time", "tower_status_radiant", "tower_status_dire",
    "barracks_status_radiant", "barracks_status_dire", "cluster", "game_mode", "lobby_type", "version",
)
MATCH_JSON = ("radiant_gold_adv", "radiant_xp_adv", "objectives", "draft_timings")
PLAYER_COLUMNS = (
    "match_id", "player_slot", "account_id", "hero_id", "kills", "deaths", "assists", "last_hits", "denies",
    "gold_per_min", "xp_per_min", "gold_spent", "net_worth", "hero_damage", "tower_damage", "hero_healing", "level",
    "stuns", "lane", "lane_role", "is_roaming", "teamfight_participation", "towers_killed", "roshans_killed",
    "obs_placed", "sen_placed", "camps_stacked", "creeps_stacked", "rune_pickups", "firstblood_claimed",
    "leaver_status", "item_0", "item_1", "item_2", "item_3", "item_4", "item_5", "backpack_0", "backpack_1",
    "backpack_2", "item_neutral",
)
PLAYER_JSON = ("gold_t", "xp_t", "lh_t", "dn_t", "times")
PLAYER_LOGS = ("kills_log", "purchase_log", "buyback_log", "runes_log", "obs_log", "sen_log", "ability_upgrades_arr",
               "neutral_item_history", "lane_pos", "killed", "killed_by", "multi_kills", "kill_streaks", "gold_reasons",
               "xp_reasons", "life_state", "item_uses")


class ExplorerError(RuntimeError):
    pass


def explorer(http: HttpClient, sql: str) -> list[dict]:
    r = http.get(f"{API}/explorer", {"sql": sql})
    js = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or js.get("err"):
        raise ExplorerError(f"{r.status_code}: {js.get('err') or r.text[:300]}")
    return js["rows"]


def match_ids(http: HttpClient, since: dt.datetime, until: dt.datetime) -> list[int]:
    """League game ids started in [since, until), ascending."""
    lo, hi = int(since.timestamp()), int(until.timestamp())
    ids, last = [], 0
    while True:
        rows = explorer(http, f"SELECT match_id FROM matches WHERE start_time >= {lo} AND start_time < {hi} "
                              f"AND match_id > {last} ORDER BY match_id LIMIT 5000")
        ids += [r["match_id"] for r in rows]
        if len(rows) < 5000:
            return ids
        last = ids[-1]


def fetch_batch(http: HttpClient, ids: list[int]) -> dict:
    """Games, players (stats + per-minute series) and heavy logs for up to BATCH game ids."""
    lst = ",".join(map(str, ids))
    matches = explorer(http, f"""SELECT {', '.join(MATCH_COLUMNS + MATCH_JSON)}, picks_bans, teamfights, mp.patch
                                 FROM matches LEFT JOIN match_patch mp USING (match_id) WHERE match_id IN ({lst})""")
    players = explorer(http, f"SELECT {', '.join(PLAYER_COLUMNS + PLAYER_JSON + PLAYER_LOGS)} "
                             f"FROM player_matches WHERE match_id IN ({lst})")
    return {"matches": matches, "players": players}


def fetch_match(http: HttpClient, match_id: int, patches: dict[int, str]) -> dict | None:
    """One game via REST /matches/{id} in the fetch_batch shape (the explorer table misses many games since mid-2026).
    None if OpenDota does not know the game; raises ExplorerError on the daily limit."""
    r = http.get(f"{API}/matches/{match_id}")
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise ExplorerError(f"{r.status_code}: {r.text[:200]} (remaining today {r.headers.get('x-rate-limit-remaining-day')})")
    m = r.json()
    m["radiant_team_name"] = (m.get("radiant_team") or {}).get("name")
    m["dire_team_name"] = (m.get("dire_team") or {}).get("name")
    m["patch"] = patches.get(m.get("patch"), m.get("patch"))
    for pb in m.get("picks_bans") or []:
        pb.setdefault("order", pb.get("ord"))
    return {"matches": [m], "players": [{**p, "match_id": match_id} for p in m.get("players") or []]}


def _j(v) -> str | None:
    return None if v is None else json.dumps(v, separators=(",", ":"))


def save_batch(conn: sqlite3.Connection, batch: dict) -> int:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    logs: dict[int, dict] = {}
    with conn:
        for m in batch["matches"]:
            mid = m["match_id"]
            conn.execute(
                f"""INSERT OR REPLACE INTO od_matches ({', '.join(MATCH_COLUMNS + MATCH_JSON)}, patch, fetched_at)
                    VALUES ({', '.join('?' * (len(MATCH_COLUMNS) + len(MATCH_JSON) + 2))})""",
                [m.get(c) for c in MATCH_COLUMNS] + [_j(m.get(c)) for c in MATCH_JSON] + [m.get("patch"), now])
            conn.execute("DELETE FROM od_picks_bans WHERE match_id=?", (mid,))
            conn.executemany("INSERT OR REPLACE INTO od_picks_bans (match_id, ord, is_pick, hero_id, team) VALUES (?,?,?,?,?)",
                             [(mid, pb.get("order"), int(bool(pb.get("is_pick"))), pb.get("hero_id"), pb.get("team"))
                              for pb in m.get("picks_bans") or []])
            logs[mid] = {"teamfights": m.get("teamfights"), "players": {}}
        for p in batch["players"]:
            conn.execute(
                f"""INSERT OR REPLACE INTO od_players ({', '.join(PLAYER_COLUMNS + PLAYER_JSON)})
                    VALUES ({', '.join('?' * (len(PLAYER_COLUMNS) + len(PLAYER_JSON)))})""",
                [p.get(c) for c in PLAYER_COLUMNS] + [_j(p.get(c)) for c in PLAYER_JSON])
            logs.setdefault(p["match_id"], {"teamfights": None, "players": {}})["players"][p["player_slot"]] = {
                c: p.get(c) for c in PLAYER_LOGS}
        conn.executemany("INSERT OR REPLACE INTO od_logs (match_id, data) VALUES (?,?)",
                         [(mid, zlib.compress(json.dumps(v, separators=(",", ":")).encode(), 6))
                          for mid, v in logs.items()])
    return len(batch["matches"])


def load_logs(conn: sqlite3.Connection, match_id: int) -> dict | None:
    """{"teamfights": [...], "players": {player_slot: {kills_log, purchase_log, ...}}}."""
    row = conn.execute("SELECT data FROM od_logs WHERE match_id=?", (match_id,)).fetchone()
    return json.loads(zlib.decompress(row[0])) if row else None


def save_reference(http: HttpClient, conn: sqlite3.Connection) -> dict[str, int]:
    """Leagues (tier), teams, team rating, notable players (country, role), heroes, items, patches."""
    leagues = explorer(http, "SELECT leagueid, name, tier FROM leagues")
    teams = explorer(http, "SELECT t.team_id, t.name, t.tag, r.rating, r.wins, r.losses, r.last_match_time "
                           "FROM teams t LEFT JOIN team_rating r USING (team_id)")
    players = explorer(http, "SELECT account_id, name, country_code, fantasy_role, team_id, team_name, team_tag, is_pro "
                             "FROM notable_players")
    heroes = http.get_json(f"{API}/heroes")
    items = http.get_json(f"{API}/constants/items")
    patches = http.get_json(f"{API}/constants/patch")
    with conn:
        conn.executemany("INSERT OR REPLACE INTO od_leagues (leagueid, name, tier) VALUES (?,?,?)",
                         [(x["leagueid"], x["name"], x["tier"]) for x in leagues])
        conn.executemany("""INSERT OR REPLACE INTO od_teams (team_id, name, tag, rating, wins, losses, last_match_time)
                            VALUES (?,?,?,?,?,?,?)""",
                         [(x["team_id"], x["name"], x["tag"], x["rating"], x["wins"], x["losses"], x["last_match_time"])
                          for x in teams])
        conn.executemany("""INSERT OR REPLACE INTO od_pro_players (account_id, name, country_code, fantasy_role, team_id,
                                team_name, team_tag, is_pro) VALUES (?,?,?,?,?,?,?,?)""",
                         [(x["account_id"], x["name"], x["country_code"], x["fantasy_role"], x["team_id"],
                           x["team_name"], x["team_tag"], x["is_pro"]) for x in players])
        conn.executemany("""INSERT OR REPLACE INTO od_heroes (id, name, localized_name, primary_attr, attack_type, roles)
                            VALUES (?,?,?,?,?,?)""",
                         [(h["id"], h["name"], h["localized_name"], h.get("primary_attr"), h.get("attack_type"),
                           _j(h.get("roles"))) for h in heroes])
        conn.executemany("INSERT OR REPLACE INTO od_items (id, name, cost) VALUES (?,?,?)",
                         [(v["id"], k, v.get("cost")) for k, v in items.items() if isinstance(v, dict) and "id" in v])
        conn.executemany("INSERT OR REPLACE INTO od_patches (id, name, date) VALUES (?,?,?)",
                         [(p["id"], p["name"], p.get("date")) for p in patches])
    return {"leagues": len(leagues), "teams": len(teams), "players": len(players), "heroes": len(heroes),
            "items": len(items), "patches": len(patches)}
