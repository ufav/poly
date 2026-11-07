"""Polymarket price history of past games (CLOB /prices-history) -> odds_history, snapshot 'history': the venue's
own prices for checking models on the markets we bet on (tennis / NBA / NHL have no bookmaker odds in the base).

For each closed main event of the target series (polymarket_prices.SERIES) of the last --days: the match / teams
(as the live collector does), the markets (football 1x2, moneyline, spreads, totals, all lines) and for each outcome
token the minute price series of kickoff - 3 days .. kickoff + 4 h (one request), kept
  - hourly until kickoff - 2 h, every minute until kickoff, every 5 minutes in play (to kickoff + 4 h);
a point is stored only when the price changed; odds = 1 / price. A two-way market (moneyline, spread, total) is
fetched by one token, the other outcome is 1 - price (the same order book mirrored); football 1x2 = three tokens. The series has no bid / ask / size (those come only
from the live collector's order book snapshots). Old closed markets answer only to time windows (startTs / endTs),
not to interval=max. Events whose markets already have history are skipped, so a run can be stopped and resumed.

  python -u -m data_engine.providers.polymarket_history [--days 60] [--series nba,atp]
"""

from __future__ import annotations

import argparse
import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import insert, text

from core.db import session
from core.models import OddsQuote
from data_engine.providers.polymarket_prices import (GENDER, SERIES, SOURCE, SPORT_ID, Resolver, closed_events, kickoff,
                                                     markets_of, series_ids, sides, upsert_market)
from pmbot.http import HttpClient
from pmbot.polymarket import CLOB

SPAN = (dt.timedelta(days=-3), dt.timedelta(hours=4))  # one request: kickoff - 3 days .. kickoff + 4 h, by minute
KEEP = [  # (from, to relative to kickoff, keep a point every N minutes)
    (dt.timedelta(days=-3), dt.timedelta(hours=-2), 60),
    (dt.timedelta(hours=-2), dt.timedelta(0), 1),
    (dt.timedelta(0), dt.timedelta(hours=4), 5),
]
WORKERS = 4


class Clob:
    def __init__(self) -> None:
        self.http = httpx.Client(timeout=30)
        self.requests = 0

    def history(self, token: str, lo: dt.datetime, hi: dt.datetime) -> list[dict]:
        for attempt in range(6):
            self.requests += 1
            try:
                r = self.http.get(f"{CLOB}/prices-history", params={
                    "market": token, "startTs": int(lo.timestamp()), "endTs": int(hi.timestamp()), "fidelity": 1})
            except httpx.TransportError:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json().get("history") or []
        raise RuntimeError(f"prices-history {token}: no answer after retries")


def series_points(clob: Clob, token: str, ko: dt.datetime) -> list[tuple[dt.datetime, float]]:
    """Minute series of one token thinned by KEEP, only the points where the price changed."""
    pts = [(dt.datetime.fromtimestamp(x["t"], dt.timezone.utc), float(x["p"]))
           for x in clob.history(token, ko + SPAN[0], ko + SPAN[1])]
    out, prev, slots = [], None, set()
    for t, p in sorted(pts):
        every = next((e for lo, hi, e in KEEP if ko + lo <= t < ko + hi), None)
        if every is None:
            continue
        slot = (every, int(t.timestamp() // (every * 60)))
        if slot in slots:
            continue
        slots.add(slot)
        if p != prev:
            out.append((t, p))
            prev = p
    return out


def fetch_market(clob: Clob, mk: dict, ko: dt.datetime) -> dict[str, list[tuple[dt.datetime, float]]]:
    """{outcome: points}; a two-way market needs one token (the other side's price is 1 - p: the same book)."""
    outcomes = list(mk["tokens"])
    if len(outcomes) == 2:
        first = series_points(clob, mk["tokens"][outcomes[0]], ko)
        return {outcomes[0]: first, outcomes[1]: [(t, round(1 - p, 5)) for t, p in first]}
    return {o: series_points(clob, tok, ko) for o, tok in mk["tokens"].items()}


def done_events(s) -> set[str]:
    return set(s.scalars(text("""
        SELECT DISTINCT mk.extra->>'event' FROM markets mk
        WHERE mk.extra ? 'event' AND EXISTS (SELECT 1 FROM odds_history o WHERE o.market_id = mk.id
                                             AND o.snapshot = 'history')""")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--series", help="comma list of series codes (default: all of polymarket_prices.SERIES)")
    args = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    http = HttpClient(min_interval=0.25)
    clob = Clob()
    pool = ThreadPoolExecutor(WORKERS)
    ids = series_ids(http)
    codes = [c.strip() for c in args.series.split(",")] if args.series else list(ids)
    with session() as s:
        done = done_events(s)
    t0 = time.time()
    total_q = total_ev = 0
    for code in codes:
        sport_id = SPORT_ID[SERIES[code][0]]
        football = SERIES[code][0] == "football"
        # games over (kickoff + 6 h in the past), so the in-play window is complete
        events = [e for e in closed_events(http, ids[code], now - dt.timedelta(hours=6), args.days)
                  if not e.get("parentEventId") and str(e["id"]) not in done
                  and any(m.get("sportsMarketType") == "moneyline" for m in e.get("markets") or [])]
        print(f"{code}: {len(events)} closed events to load", flush=True)
        with session() as s:
            r = Resolver(s)  # loads every team name once per series
            league_id = r.league(code)

        def fetch_event(e: dict) -> tuple:
            home, away = sides(e, sport_id)
            mks = markets_of(e, home, away, football, closed=True)
            return home, away, mks, [(mk, fetch_market(clob, mk, kickoff(e))) for mk in mks]

        events.sort(key=kickoff)
        for n, (e, (home, away, mks, got)) in enumerate(zip(events, pool.map(fetch_event, events)), 1):
            ko = kickoff(e)
            with session() as s:
                r.s = s
                g = GENDER.get(code, "m")
                hid = r.team(sport_id, home["key"], home["name"], home.get("alias"), SERIES[code][1], g)
                aid = r.team(sport_id, away["key"], away["name"], away.get("alias"), SERIES[code][1], g)
                mid = r.match(e, sport_id, league_id, hid, aid, ko)
                market_id = {id(mk): upsert_market(s, mid, mk, e["id"]) for mk in mks}
                rows = [{"market_id": market_id[id(mk)], "outcome": outcome, "source_id": SOURCE,
                         "bookmaker": "polymarket", "price": round(p, 5), "odds": round(1 / p, 3) if p > 0 else None,
                         "snapshot": "history", "taken_at": t}
                        for mk, by_outcome in got for outcome, pts in by_outcome.items() for t, p in pts]
                for c in range(0, len(rows), 5000):
                    s.execute(insert(OddsQuote), rows[c:c + 5000])
            total_q += len(rows)
            total_ev += 1
            if n % 25 == 0 or n == len(events):
                print(f"  [{n:5d}/{len(events)}] {ko:%Y-%m-%d} {e['title'][:40]:<40} markets {len(mks):3d}  "
                      f"points {len(rows):5d}  total {total_q:9d}  requests {clob.requests:7d}  "
                      f"{(time.time() - t0) / 60:5.1f} min", flush=True)
    print(f"done: {total_ev} events, {total_q} price points, {clob.requests} requests in "
          f"{(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
