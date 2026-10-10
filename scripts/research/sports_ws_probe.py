"""Listen to the Polymarket sports WebSocket and print CS2 score updates with arrival time.

  python scripts/research/sports_ws_probe.py [seconds]
"""

import asyncio
import datetime as dt
import json
import sys

import websockets

URL = "wss://sports-api.polymarket.com/ws"


async def main(seconds: int) -> None:
    last = {}
    leagues = set()
    async with websockets.connect(URL, ping_interval=None) as ws:
        end = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            if msg == "ping":
                await ws.send("pong")
                continue
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                print("raw:", msg[:200])
                continue
            for item in data if isinstance(data, list) else [data]:
                league = str(item.get("leagueAbbreviation") or "").lower()
                leagues.add(league)
                if league not in ("cs2", "csgo", "cs"):
                    continue
                slug = f"{item.get('gameId')} {item.get('homeTeam')} vs {item.get('awayTeam')}"
                key = (item.get("score"), item.get("period"))
                if last.get(slug) != key:
                    now = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
                    print(now, slug, key, {k: item.get(k) for k in ("live", "ended", "elapsed", "status")})
                    last[slug] = key
    print("leagues seen:", sorted(leagues))


asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 90))
