"""PandaScore free-tier list endpoints: map timing (games begin_at/end_at), rounds, /lives websocket info.

  python scripts/research/pandascore_probe2.py
"""

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
API = "https://api.pandascore.co"
client = httpx.Client(headers={"Authorization": f"Bearer {os.environ['PANDASCORE_ACCESS_TOKEN']}"}, timeout=30)


def get(path: str, params: dict | None = None):
    r = client.get(f"{API}{path}", params=params)
    print(f"\n=== GET {path} {params or ''} -> {r.status_code}  remaining={r.headers.get('x-rate-limit-remaining')}")
    if r.status_code != 200:
        print("   ", r.text[:200])
        return None
    return r.json()


ms = get("/csgo/matches", {"filter[id]": "1685172,1700519"})
for m in ms or []:
    print(f"   {m['id']} {m['name']} begin {m.get('begin_at')} end {m.get('end_at')} status {m.get('status')}")
    for g in m.get("games") or []:
        print("     game", {k: g.get(k) for k in ("id", "position", "status", "begin_at", "end_at", "length", "finished",
                                                 "winner", "complete", "detailed_stats")})
    print("   match keys:", sorted(m.keys()))
    gid = (m.get("games") or [{}])[0].get("id")
    if gid:
        get(f"/csgo/games/{gid}/rounds")
        get(f"/csgo/games/{gid}")

lives = get("/lives")
for x in lives or []:
    print("   live:", x["match"]["id"], x["match"]["name"], "| endpoints:",
          json.dumps({k: v for k, v in x.items() if k != "match"}, ensure_ascii=False)[:400])
