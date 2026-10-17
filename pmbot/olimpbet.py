"""Olimpbet (old.olimpbet.kz) results JSON API: tennis matches of a day with tournament, players and set scores.

- GET /api/v2/event-results?locale=en&sport-ids=101&scheduled-from=..&scheduled-to=..&page-size=100[&page-key=..]
  -> {items, paginationKeyForward}; item: tournament {id, name, countryId}, competitors [{id, name, countryId}],
     homeCompetitorIds, eventDate (UTC), basicStatistics.scoresByPeriod (sets), currentScore (sets won), status.
Tournament name carries level, city, country and surface: 'ATP Challenger. Columbus. USA. Hard',
'World Tennis W15. Monastir. Tunisia. Hard', '... Doubles', 'UTR PTT. Newport Beach. USA. Hard. Women'.
Behind Cloudflare: plain requests pass, keep a slow pace (the site is protected against scraping).
"""

import datetime as dt
import json
import sqlite3

import httpx

from pmbot.http import HttpClient

API = "https://old.olimpbet.kz/api/v2/event-results"
TENNIS_SPORT_ID = 101
SITE_TZ = "+05:00"  # day boundaries as on the site (Kazakhstan)
SURFACES = ("Hard", "Clay", "Grass", "Carpet")
STATUSES = ("TRADING", "SUSPENDED", "OPEN", "CANCELLED", "INTERRUPTED", "POSTPONED", "ENDED", "CLOSED")
HEADERS = {"Accept": "application/json, text/plain, */*", "Accept-Language": "ru,en;q=0.9"}


def client(min_interval: float) -> HttpClient:
    return HttpClient(min_interval=min_interval, retries=3, timeout=30, headers=HEADERS)


PAGE_SIZES = (100, 20, 5)


class ServerError(Exception):
    """HTTP 5xx (Cloudflare 520): the origin fails on some item of the page - deterministic, retry smaller pages."""


def day_url(day: dt.date, sport_id: int = TENNIS_SPORT_ID, hours: tuple[int, int] = (0, 23),
            page_size: int = 100) -> str:
    """The same query the results page sends, in English; `hours` = site-time window inside the day."""
    d = day.isoformat()
    tz = SITE_TZ.replace("+", "%2B").replace(":", "%3A")
    return (f"{API}?locale=en&sport-ids={sport_id}&include-subsports=true&tournament-ids="
            f"&scheduled-from={d}T{hours[0]:02d}%3A00%3A00{tz}&scheduled-to={d}T{hours[1]:02d}%3A59%3A59{tz}"
            "&completion-status=ENDED" + "".join(f"&statuses={s}" for s in STATUSES)
            + f"&sort=tournamentId.asc&sort=scheduledAt.asc&page-size={page_size}")


def _fetch_pages(http: HttpClient, url: str) -> tuple[list[dict], int]:
    items, pages, key = [], 0, None
    while True:
        try:
            r = http.get(url + (f"&page-key={key}" if key else ""))
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                raise ServerError(str(e.response.status_code)) from e
            raise
        if r.status_code >= 500:
            raise ServerError(str(r.status_code))
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        doc = r.json()
        pages += 1
        items += doc.get("items", [])
        key = doc.get("paginationKeyForward")
        if not key or not doc.get("items"):
            return items, pages


def fetch_day(http: HttpClient, day: dt.date) -> tuple[list[dict], int, int]:
    """All result items of a day, requests made, and hours lost. On a server error the day is retried with smaller
    pages, then hour by hour (an hour that still fails is skipped and counted). Blocking errors (403 / 429 /
    network) are raised to the caller."""
    requests = 0
    for size in PAGE_SIZES:
        try:
            items, pages = _fetch_pages(http, day_url(day, page_size=size))
            return items, requests + pages, 0
        except ServerError:
            requests += 1
    items, failed = [], 0
    for h in range(24):
        try:
            got, pages = _fetch_pages(http, day_url(day, hours=(h, h), page_size=PAGE_SIZES[-1]))
            items += got
            requests += pages
        except ServerError:
            requests += 1
            failed += 1
    return list({x["id"]: x for x in items}.values()), requests, failed


def parse_tournament(name: str) -> dict:
    """'ATP Challenger. Columbus. USA. Hard' -> level / city / country / surface; doubles and women flags."""
    parts = [p.strip() for p in name.split(". ")]
    surface_at = next((i for i, p in enumerate(parts) if p in SURFACES), None)
    head = parts[:surface_at] if surface_at is not None else parts
    return {
        "level": head[0] if head else None,
        "city": head[1] if len(head) >= 3 else None,
        "country": head[-1] if len(head) >= 3 else None,
        "surface": parts[surface_at] if surface_at is not None else None,
        "doubles": int(any(p.lower().startswith("doubles") for p in parts)),
        "women": int(any("women" in p.lower() for p in parts) or (parts[0].startswith("World Tennis W"))
                     or parts[0].startswith("WTA")),
    }


def save_day(conn: sqlite3.Connection, day: dt.date, items: list[dict], pages: int, failed_hours: int = 0) -> None:
    with conn:
        for e in items:
            t = e["tournament"]
            conn.execute(
                """INSERT INTO ob_tournaments (id, name, country_id, level, city, country, surface, doubles, women)
                   VALUES (:id, :name, :country_id, :level, :city, :country, :surface, :doubles, :women)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name""",
                {"id": t["id"], "name": t["name"], "country_id": t.get("countryId"), **parse_tournament(t["name"])})
            comp = {c["id"]: c for c in e.get("competitors", [])}
            home_ids = e.get("homeCompetitorIds") or []
            home = next((comp[i] for i in home_ids if i in comp), None)
            away = next((c for i, c in comp.items() if i not in home_ids), None)
            stats = e.get("basicStatistics") or {}
            cur = stats.get("currentScore") or {}
            sets = [(s.get("homeScore"), s.get("awayScore")) for s in stats.get("scoresByPeriod") or []]
            conn.execute(
                """INSERT OR REPLACE INTO ob_events (id, tournament_id, day, scheduled_at, home_id, home, home_country_id,
                       away_id, away, away_country_id, sets_home, sets_away, set_scores, status, completion_status,
                       event_type, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (e["id"], t["id"], day.isoformat(), e.get("eventDate"),
                 home and home["id"], home and home["name"], home and home.get("countryId"),
                 away and away["id"], away and away["name"], away and away.get("countryId"),
                 cur.get("homeScore"), cur.get("awayScore"), json.dumps(sets), e.get("status"),
                 e.get("completionStatus"), (e.get("eventType") or {}).get("name"),
                 json.dumps(e, ensure_ascii=False)))
        conn.execute("INSERT OR REPLACE INTO ob_days (day, events, pages, failed_hours, fetched_at) VALUES (?,?,?,?,?)",
                     (day.isoformat(), len(items), pages, failed_hours, dt.datetime.now(dt.timezone.utc).isoformat()))
