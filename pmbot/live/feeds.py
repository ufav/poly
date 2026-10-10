"""Live data sources: Polymarket sports WebSocket (round score), Gamma (events, markets, settlement), CLOB order book."""

import asyncio
import datetime as dt
import json
import logging
import re
from collections.abc import AsyncIterator

import websockets

from pmbot.http import HttpClient
from pmbot.polymarket import CLOB, CS2_SERIES_ID, GAMMA, _title_parts, parse_ts

SPORTS_WS = "wss://sports-api.polymarket.com/ws"
CS2_LEAGUES = {"cs2", "csgo"}
log = logging.getLogger(__name__)


def parse_event(event: dict) -> dict:
    best_of, _ = _title_parts(event["title"])
    markets = []
    for m in event.get("markets", []):
        mtype = m.get("sportsMarketType")
        if mtype not in ("moneyline", "child_moneyline"):
            continue
        map_no = re.match(r"Map (\d+)", m.get("groupItemTitle") or "")
        outcomes = json.loads(m.get("outcomes") or "[null, null]")
        tokens = json.loads(m.get("clobTokenIds") or "[null, null]")
        markets.append({
            "market_id": m["id"], "market_type": mtype, "map_number": int(map_no.group(1)) if map_no else None,
            "outcome_a": outcomes[0], "outcome_b": outcomes[1], "token_a": tokens[0], "token_b": tokens[1],
        })
    return {
        "game_id": event.get("gameId"), "event_id": event["id"], "slug": event.get("slug"), "title": event["title"],
        "best_of": best_of, "start_time": event.get("startTime") or event.get("startDate"), "markets": markets,
    }


def open_events(http: HttpClient, days_back: int = 2) -> list[dict]:
    """Not yet closed CS2 events that started recently or are upcoming (old unresolved ones are skipped)."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events, offset = [], 0
    while True:
        page = http.get_json(f"{GAMMA}/events", {"series_id": CS2_SERIES_ID, "closed": "false", "limit": 100,
                                                 "offset": offset, "start_date_min": since})
        events += page
        if len(page) < 100:
            return events
        offset += 100


def fetch_book(http: HttpClient, token: str) -> dict:
    book = http.get_json(f"{CLOB}/book", {"token_id": token})
    bids = sorted(((float(x["price"]), float(x["size"])) for x in book.get("bids", [])), reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in book.get("asks", []))
    return {
        "bids": bids, "asks": asks,
        "bid": bids[0][0] if bids else None, "bid_size": bids[0][1] if bids else None,
        "ask": asks[0][0] if asks else None, "ask_size": asks[0][1] if asks else None,
    }


def fetch_market(http: HttpClient, market_id: str) -> dict:
    return http.get_json(f"{GAMMA}/markets/{market_id}")


def prematch_price(http: HttpClient, token: str, start_time: str | None) -> float | None:
    """Price of token at scheduled start (last history point before it); current midpoint if not started yet."""
    start = parse_ts(start_time)
    now = dt.datetime.now(dt.timezone.utc)
    if start and start < now:
        r = http.get(f"{CLOB}/prices-history", {"market": token, "startTs": int(start.timestamp()) - 6 * 3600,
                                                "endTs": int(start.timestamp()), "fidelity": 1})
        hist = r.json().get("history", []) if r.status_code == 200 else []
        if hist:
            return float(hist[-1]["p"])
    book = fetch_book(http, token)
    if book["bid"] is not None and book["ask"] is not None:
        return (book["bid"] + book["ask"]) / 2
    return None


async def sports_updates() -> AsyncIterator[dict]:
    """CS2 items from the sports WebSocket: gameId, homeTeam, awayTeam, score, period, status, live, ended.
    Reconnects forever; the server sends 'ping' text frames that must be answered with 'pong'."""
    delay = 1
    while True:
        try:
            async with websockets.connect(SPORTS_WS, ping_interval=None, open_timeout=20) as ws:
                log.info("sports ws connected")
                delay = 1
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=120)
                    if msg == "ping":
                        await ws.send("pong")
                        continue
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        continue
                    for item in data if isinstance(data, list) else [data]:
                        if str(item.get("leagueAbbreviation") or "").lower() in CS2_LEAGUES:
                            yield item
        except Exception as e:  # network errors, timeouts, server closes
            log.warning("sports ws error: %r; reconnect in %ss", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
