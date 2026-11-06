"""Football results from the Olimpbet results API -> data/football.sqlite (ob_fb_events), day by day (site time
+05:00), all tournaments: matches (score, halves, red cards, added time) and statistics events (corners, cards,
shots, shots on target, fouls, offsides ... by half) linked to the match by base_event_id. Continues the local Olimp
archive (2008-03 ... 2022-10, scripts/import_olimp_football.py). A re-run skips finished days; the last 3 days are
fetched again.

  python -u scripts/collect_olimpbet_football.py [--since 2022-10-25] [--until YYYY-MM-DD] [--interval 2.5]
"""

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import footballdata as fd  # noqa: E402
from pmbot import olimpbet as ob  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS ob_fb_events (
    id             INTEGER PRIMARY KEY,
    base_event_id  INTEGER,  -- statistics event -> its match id
    date           TEXT,     -- UTC ISO
    tournament_id  INTEGER,
    tournament     TEXT,
    kind           TEXT,     -- GENERAL / STATISTICS
    event_type     TEXT,     -- MAIN / Corners / Yellow cards / Shots ...
    home_id INTEGER, away_id INTEGER, home TEXT, away TEXT,
    hs INTEGER, as_ INTEGER, -- score (stat count for statistics events)
    periods        TEXT,     -- JSON [[home, away], ...] by half
    home_red INTEGER, away_red INTEGER,
    add_minutes    TEXT,
    status         TEXT
);
CREATE INDEX IF NOT EXISTS ob_fb_events_base ON ob_fb_events (base_event_id);
CREATE INDEX IF NOT EXISTS ob_fb_events_date ON ob_fb_events (date);
CREATE TABLE IF NOT EXISTS ob_fb_days (
    day TEXT PRIMARY KEY, items INTEGER, requests INTEGER, failed_hours INTEGER, fetched_at REAL
);
"""
FIELDS = ["id", "base_event_id", "date", "tournament_id", "tournament", "kind", "event_type", "home_id", "away_id",
          "home", "away", "hs", "as_", "periods", "home_red", "away_red", "add_minutes", "status"]


def bar(done: int, total: int, width: int = 30) -> str:
    filled = int(width * done / total) if total else width
    return f"[{'#' * filled}{'.' * (width - filled)}] {100 * done / total if total else 100:5.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2022-10-25", help="first day (archive ends 2022-10)")
    ap.add_argument("--until", help="last day (default yesterday)")
    ap.add_argument("--interval", type=float, default=2.5, help="seconds between requests (site is protected)")
    args = ap.parse_args()

    conn = fd.connect()
    conn.executescript(SCHEMA)
    first = dt.date.fromisoformat(args.since)
    last = dt.date.fromisoformat(args.until) if args.until else dt.date.today() - dt.timedelta(days=1)
    done = {d for (d,) in conn.execute("SELECT day FROM ob_fb_days WHERE failed_hours = 0")}
    recent = last - dt.timedelta(days=2)
    days = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]
    todo = [d for d in days if d.isoformat() not in done or d >= recent]
    print(f"Olimpbet football {first} .. {last} -> {fd.DB}: {len(todo)} days to fetch "
          f"({len(days) - len(todo)} done), ~5 requests a day, {args.interval:g}s apart", flush=True)

    http = ob.client(min_interval=args.interval)
    t0, total_items = time.time(), 0
    for i, day in enumerate(todo, 1):
        try:
            items, requests, failed = ob.fetch_day(http, day, sport_id=ob.FOOTBALL_SPORT_ID, page_sizes=(500, 100, 20))
        except (httpx.HTTPStatusError, RuntimeError) as e:  # 403 / 429: blocked - stop, re-run later
            print(f"\nstopped at {day}: {e!r:.200}\nre-run later (finished days are kept)", flush=True)
            return
        rows = [r for r in map(ob.parse_football, items) if r]
        with conn:
            conn.executemany(f"INSERT OR REPLACE INTO ob_fb_events ({', '.join(FIELDS)}) "
                             f"VALUES ({', '.join('?' * len(FIELDS))})", [tuple(r[f] for f in FIELDS) for r in rows])
            conn.execute("INSERT OR REPLACE INTO ob_fb_days VALUES (?,?,?,?,?)",
                         (day.isoformat(), len(rows), requests, failed, time.time()))
        total_items += len(rows)
        matches = sum(r["kind"] != "STATISTICS" for r in rows)
        elapsed = time.time() - t0
        eta = elapsed / i * (len(todo) - i)
        lost = f"  {failed}h lost" if failed else ""
        print(f"{bar(i, len(todo))} {i:4d}/{len(todo)}  {day}  {matches:5d} matches {len(rows) - matches:5d} stats"
              f"{lost}   total {total_items:8d}  ETA {int(eta // 3600)}h{int(eta % 3600 // 60):02d}m", flush=True)

    n, m, a, b = conn.execute("SELECT COUNT(*), SUM(kind != 'STATISTICS'), MIN(date), MAX(date) "
                              "FROM ob_fb_events").fetchone()
    print(f"\ndone in {(time.time() - t0) / 3600:.1f} h; database: {n} events ({m} matches), {a} .. {b}")


if __name__ == "__main__":
    main()
