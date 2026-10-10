"""What the PandaScore token gives: match by Polymarket gameId, games (maps), rounds with timestamps, live endpoints.

  python scripts/research/pandascore_probe.py
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
API = "https://api.pandascore.co"
client = httpx.Client(headers={"Authorization": f"Bearer {os.environ['PANDASCORE_ACCESS_TOKEN']}",
                               "Accept": "application/json"}, timeout=30)


def get(path: str, params: dict | None = None):
    r = client.get(f"{API}{path}", params=params)
    limits = {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()}
    print(f"\n=== GET {path} -> {r.status_code} {limits}")
    if r.status_code != 200:
        print("   ", r.text[:300])
        return None
    return r.json()


conn = sqlite3.connect(f"file:{ROOT / 'data' / 'cs2.sqlite'}?mode=ro", uri=True)
game_id = conn.execute("SELECT game_id FROM pm_events WHERE id = '1044738'").fetchone()[0]
print("Polymarket gameId for Aurora vs Vitality 2026-09-19:", game_id)

match = get(f"/csgo/matches/{game_id}")
if match:
    print("   name:", match.get("name"), "| begin_at:", match.get("begin_at"), "| status:", match.get("status"))
    print("   keys:", sorted(match.keys()))
    games = match.get("games") or []
    print("   games:", [(g.get("id"), g.get("position"), g.get("status"), (g.get("map") or {}).get("name")) for g in games])
    if games:
        gid = games[0]["id"]
        game = get(f"/csgo/games/{gid}")
        if game:
            print("   game keys:", sorted(game.keys()))
            rounds = game.get("rounds") or []
            print("   rounds in game:", len(rounds), json.dumps(rounds[:2], ensure_ascii=False)[:600])
        rounds = get(f"/csgo/games/{gid}/rounds")
        if rounds:
            print("   rounds:", len(rounds), json.dumps(rounds[:2], ensure_ascii=False)[:800])
        events = get(f"/csgo/games/{gid}/events", {"per_page": 5})
        if events:
            print("   events:", json.dumps(events[:3], ensure_ascii=False)[:800])

running = get("/csgo/matches/running")
if running is not None:
    print("   running:", [(m.get("id"), m.get("name")) for m in running][:10])
lives = get("/lives")
if lives is not None:
    print("   lives:", json.dumps(lives[:2], ensure_ascii=False)[:600])
past = get("/csgo/matches/past", {"per_page": 2})
if past is not None:
    print("   past ok:", [(m.get("id"), m.get("name")) for m in past])
