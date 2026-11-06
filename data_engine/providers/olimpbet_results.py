"""Olimpbet results API (old.olimpbet.kz/api/v2/event-results, the site's results page) -> PostgreSQL raw_payloads,
one row per sport and day (site time +05:00): kind 'results_en', external_id 'football|2026-11-05', payload
{"items": [...]} with each event trimmed to what normalization needs (ids, tournament, competitors, date, scores by
period, live statistics, statistics events -> baseEventId). The site keeps scores only ~14 months back, so the
history has to be collected continuously.

Russian names (kind 'results_ru') are fetched for a weekly sample of days and for every day that brings competitors
without a Russian name yet: they equal the names of the local Olimp archive (2008-2023) and link it to the API ids.

  python -u -m data_engine.providers.olimpbet_results --backfill [--days 430] [--sports football,hockey]
  python -u -m data_engine.providers.olimpbet_results --daily      # last 3 days, for a daily scheduled run
"""

from __future__ import annotations

import argparse
import datetime as dt
import time

from sqlalchemy import delete, insert, select, text

from core.db import session
from core.models import RawPayload
from pmbot import olimpbet as ob

SOURCE = 3  # olimpbet
SPORTS = {"football": 100, "tennis": 101, "basketball": 102, "hockey": 103, "volleyball": 104}
RU_SAMPLE_STEP = 7
REFRESH_DAYS = 3


def compact(item: dict) -> dict:
    """Event without the bulky fields (linked tournaments, weights, comments, outcomes)."""
    t = item.get("tournament") or {}
    return {
        "id": item.get("id"), "baseEventId": item.get("baseEventId"), "kind": item.get("kind"),
        "eventType": (item.get("eventType") or {}).get("name"), "eventDate": item.get("eventDate"),
        "status": item.get("status"), "completionStatus": item.get("completionStatus"),
        "tournament": {"id": t.get("id"), "name": t.get("name"), "kind": t.get("kind"),
                       "sportId": t.get("sportId"), "baseTournamentId": t.get("baseTournamentId")},
        "competitors": [{"id": c.get("id"), "name": c.get("name"), "type": c.get("type")}
                        for c in item.get("competitors") or []],
        "homeCompetitorIds": item.get("homeCompetitorIds"),
        "basicStatistics": item.get("basicStatistics"),
        "liveStatistics": item.get("liveStatistics") or None,
    }


def fetch(http, day: dt.date, sport_id: int, locale: str) -> tuple[list[dict], int]:
    if locale == "en":
        items, requests, _ = ob.fetch_day(http, day, sport_id=sport_id, page_sizes=(500, 100, 20))
        return items, requests
    url = ob.day_url(day, sport_id=sport_id, page_size=500).replace("locale=en", "locale=ru")
    return ob._fetch_pages(http, url)


def store(s, sport: str, day: dt.date, locale: str, items: list[dict]) -> None:
    key = f"{sport}|{day.isoformat()}"
    kind = f"results_{locale}"
    s.execute(delete(RawPayload).where(RawPayload.source_id == SOURCE, RawPayload.kind == kind,
                                       RawPayload.external_id == key))
    s.execute(insert(RawPayload).values(source_id=SOURCE, kind=kind, external_id=key,
                                        payload={"items": [compact(i) for i in items]}))


def known(s, locale: str) -> set[str]:
    return set(s.scalars(select(RawPayload.external_id).where(RawPayload.source_id == SOURCE,
                                                              RawPayload.kind == f"results_{locale}")))


def ru_competitors(s, sport: str) -> set[int]:
    """Competitor ids that already have a Russian name for this sport."""
    rows = s.execute(text("""
        SELECT DISTINCT (c->>'id')::bigint FROM raw_payloads r,
          jsonb_array_elements(r.payload->'items') i, jsonb_array_elements(i->'competitors') c
        WHERE r.source_id = :src AND r.kind = 'results_ru' AND r.external_id LIKE :pfx"""),
                     {"src": SOURCE, "pfx": f"{sport}|%"})
    return {r[0] for r in rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--backfill", action="store_true", help="every day of the last --days not fetched yet")
    mode.add_argument("--daily", action="store_true", help=f"the last {REFRESH_DAYS} days again + missing ones")
    ap.add_argument("--days", type=int, default=430, help="backfill depth (scores are kept ~14 months)")
    ap.add_argument("--sports", default=",".join(SPORTS))
    ap.add_argument("--interval", type=float, default=2.5, help="seconds between requests (site is protected)")
    args = ap.parse_args()

    sports = [x.strip() for x in args.sports.split(",") if x.strip()]
    yesterday = dt.date.today() - dt.timedelta(days=1)
    first = yesterday - dt.timedelta(days=args.days - 1)
    days = [first + dt.timedelta(days=i) for i in range(args.days)]
    with session() as s:
        have_en, have_ru = known(s, "en"), known(s, "ru")
        ru_ids = {sp: ru_competitors(s, sp) for sp in sports}
    refresh = {yesterday - dt.timedelta(days=i) for i in range(REFRESH_DAYS)}
    todo = [(sp, d) for d in days for sp in sports
            if f"{sp}|{d.isoformat()}" not in have_en or (args.daily and d in refresh)]
    if args.daily:
        todo = [(sp, d) for sp, d in todo if d >= first]
    print(f"Olimpbet results -> PostgreSQL raw_payloads: {len(todo)} sport-days to fetch "
          f"({', '.join(sports)}; {first} .. {yesterday})", flush=True)

    http = ob.client(min_interval=args.interval)
    t0 = time.time()
    for n, (sport, day) in enumerate(todo, 1):
        try:
            items, requests = fetch(http, day, SPORTS[sport], "en")
            new_ids = {c["id"] for i in items for c in i.get("competitors") or []} - ru_ids[sport]
            sample = (day - first).days % RU_SAMPLE_STEP == 0
            ru_items = None
            if (sample or len(new_ids) > 0) and f"{sport}|{day.isoformat()}" not in have_ru:
                ru_items, r2 = fetch(http, day, SPORTS[sport], "ru")
                requests += r2
        except Exception as e:  # blocked / server error: stop; a re-run continues
            print(f"\nstopped at {sport} {day}: {e!r:.200}\nre-run later (stored days are kept)", flush=True)
            return
        with session() as s:
            store(s, sport, day, "en", items)
            if ru_items is not None:
                store(s, sport, day, "ru", ru_items)
                ru_ids[sport] |= {c["id"] for i in ru_items for c in i.get("competitors") or []}
        scored = sum(1 for i in items if (i.get("basicStatistics") or {}).get("currentScore"))
        eta = (time.time() - t0) / n * (len(todo) - n)
        print(f"[{n:5d}/{len(todo)}] {sport:<10} {day}  {len(items):5d} events ({scored:5d} scored)"
              f"{'  +ru' if ru_items is not None else ''}  {requests} req   "
              f"ETA {int(eta // 3600)}h{int(eta % 3600 // 60):02d}m", flush=True)
    print(f"done in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
