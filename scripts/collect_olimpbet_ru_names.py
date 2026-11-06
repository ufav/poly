"""Russian names of Olimpbet football competitors and tournaments (results API with locale=ru) -> data/football.sqlite
(ob_fb_names_ru). They equal the names of the Olimp archive (2008-2022), which links the archive teams (Russian names)
to the API teams (stable ids, English names, 2022-2026). One day a week over the API period, the weekday rotating so
that midweek teams appear too; a re-run skips sampled days already fetched.

  python -u scripts/collect_olimpbet_ru_names.py [--since 2022-10-25] [--step 7] [--interval 2.5]
"""

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import footballdata as fd  # noqa: E402
from pmbot import olimpbet as ob  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS ob_fb_names_ru (
    kind  TEXT,     -- competitor / tournament
    id    INTEGER,
    name  TEXT,
    seen  INTEGER,  -- sampled days it appeared on
    PRIMARY KEY (kind, id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ob_fb_names_ru_days (day TEXT PRIMARY KEY, items INTEGER, fetched_at REAL);
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2022-10-25")
    ap.add_argument("--until", help="default yesterday")
    ap.add_argument("--step", type=int, default=7, help="days between samples")
    ap.add_argument("--interval", type=float, default=2.5)
    args = ap.parse_args()
    conn = fd.connect()
    conn.executescript(SCHEMA)
    first = dt.date.fromisoformat(args.since)
    last = dt.date.fromisoformat(args.until) if args.until else dt.date.today() - dt.timedelta(days=1)
    days, d, i = [], first, 0
    while d <= last:
        days.append(d + dt.timedelta(days=i % args.step))  # rotate the weekday
        d += dt.timedelta(days=args.step)
        i += 1
    done = {x for (x,) in conn.execute("SELECT day FROM ob_fb_names_ru_days")}
    todo = [x for x in days if x <= last and x.isoformat() not in done]
    print(f"Olimpbet ru names: {len(todo)} sampled days to fetch ({len(days) - len(todo)} done)", flush=True)
    http = ob.client(min_interval=args.interval)
    t0 = time.time()
    for n, day in enumerate(todo, 1):
        items, pages = [], 0
        url = ob.day_url(day, sport_id=ob.FOOTBALL_SPORT_ID, page_size=500).replace("locale=en", "locale=ru")
        try:
            items, pages = ob._fetch_pages(http, url)
        except Exception as e:  # blocked / server error: stop, re-run later
            print(f"stopped at {day}: {e!r:.200}", flush=True)
            return
        with conn:
            for it in items:
                t = it.get("tournament") or {}
                if t.get("id"):
                    conn.execute("INSERT INTO ob_fb_names_ru VALUES ('tournament', ?, ?, 1) ON CONFLICT (kind, id) "
                                 "DO UPDATE SET seen = seen + 1", (t["id"], t.get("name")))
                for c in it.get("competitors") or []:
                    conn.execute("INSERT INTO ob_fb_names_ru VALUES ('competitor', ?, ?, 1) ON CONFLICT (kind, id) "
                                 "DO UPDATE SET seen = seen + 1", (c["id"], c.get("name")))
            conn.execute("INSERT OR REPLACE INTO ob_fb_names_ru_days VALUES (?,?,?)",
                         (day.isoformat(), len(items), time.time()))
        known = conn.execute("SELECT COUNT(*) FROM ob_fb_names_ru WHERE kind = 'competitor'").fetchone()[0]
        eta = (time.time() - t0) / n * (len(todo) - n)
        print(f"[{n:4d}/{len(todo)}] {day}  {len(items):5d} events  competitors known {known:6d}   "
              f"ETA {int(eta // 60)}m{int(eta % 60):02d}s", flush=True)


if __name__ == "__main__":
    main()
