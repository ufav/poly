"""Can the token open PandaScore live WebSockets (frames / events)? Prints the first messages.

  python scripts/research/pandascore_live_probe.py [seconds]
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import websockets
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
TOKEN = os.environ["PANDASCORE_ACCESS_TOKEN"]


async def listen(url: str, seconds: int) -> None:
    try:
        async with websockets.connect(f"{url}?token={TOKEN}", open_timeout=15) as ws:
            print(f"connected {url}")
            end = asyncio.get_event_loop().time() + seconds
            n = 0
            while asyncio.get_event_loop().time() < end and n < 4:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                n += 1
                print(f"  [{url.rsplit('/', 1)[-1]}] {str(msg)[:700]}")
    except Exception as e:
        print(f"{url}: {e!r}"[:300])


async def main(seconds: int) -> None:
    lives = httpx.get("https://api.pandascore.co/lives", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30).json()
    cs = [x for x in lives if x["match"]["videogame"]["slug"] == "cs-go"]
    print("live CS matches:", [(x["match"]["id"], x["match"]["name"], x["match"]["status"]) for x in cs])
    running = [x for x in cs if x["match"]["status"] == "running"] or cs
    if not running:
        return
    urls = [e["url"] for e in running[0]["endpoints"]]
    await asyncio.gather(*(listen(u, seconds) for u in urls))


asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
