"""Olimpbet results by day -> data/<sport>.sqlite (ob_tournaments, ob_events, ob_days).

Default sport=tennis (sport-id 101). Volleyball sport-id on Olimpbet is not wired yet (--sport-id when known).

Slow on purpose (the site protects against scraping): DELAY s +-30% between requests. A server error (520) on a day
is a broken item, not a block: the day is re-read with smaller pages, then hour by hour (lost hours are counted in
ob_days.failed_hours). Blocking errors (403 / 429 / network) wait 1 / 5 / 15 min, then stop. Re-running resumes
(days in ob_days are skipped; the last 2 days are always refreshed).

  python scripts/collect_olimpbet.py [--sport tennis] [--since 2025-10-01] [--until YYYY-MM-DD] [--delay 4]
  python scripts/collect_olimpbet.py --sport volleyball --sport-id NNN --since 2025-10-01
"""

import argparse
import datetime as dt
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import olimpbet, storage  # noqa: E402

BACKOFF_MIN = (1, 5, 15)


def fmt_eta(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60} ч {m % 60:02d} мин" if m >= 60 else f"{m} мин"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", choices=("tennis", "volleyball"), default="tennis")
    ap.add_argument("--sport-id", type=int, default=None,
                    help="override Olimpbet sport-ids (defaults: tennis=101, volleyball=104)")
    ap.add_argument("--since", default="2025-10-01")
    ap.add_argument("--until", default=None, help="default: today")
    ap.add_argument("--delay", type=float, default=4.0, help="seconds between requests (randomized +-30%%)")
    args = ap.parse_args()

    defaults = {"tennis": olimpbet.TENNIS_SPORT_ID, "volleyball": olimpbet.VOLLEYBALL_SPORT_ID}
    sport_id = args.sport_id if args.sport_id is not None else defaults[args.sport]

    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until) if args.until else dt.date.today()
    conn = storage.connect(storage.db_path(args.sport))
    done = {r[0] for r in conn.execute("SELECT day FROM ob_days WHERE day < ?",
                                       ((dt.date.today() - dt.timedelta(days=1)).isoformat(),))}
    days = [since + dt.timedelta(days=i) for i in range((until - since).days + 1)]
    todo = [d for d in days if d.isoformat() not in done]
    print(f"Olimpbet {args.sport} (sport-id={sport_id}): дней {len(days)}, уже собрано {len(days) - len(todo)}, "
          f"осталось {len(todo)}; пауза ~{args.delay:g} с", flush=True)

    http = olimpbet.client(min_interval=0)
    started, total_events, total_pages, failures = time.monotonic(), 0, 0, 0
    try:
        i = 0
        while i < len(todo):
            day = todo[i]
            try:
                items, pages, lost = olimpbet.fetch_day(_Paced(http, args.delay), day, sport_id=sport_id)
            except Exception as e:
                if failures >= len(BACKOFF_MIN):
                    print(f"\nОстановка: {failures + 1} ошибки подряд, последняя: {e}. Запустите позже — "
                          f"продолжит с {day}.", flush=True)
                    return
                wait = BACKOFF_MIN[failures]
                failures += 1
                print(f"\n  {day}: {e} — жду {wait} мин", flush=True)
                time.sleep(wait * 60)
                continue
            failures = 0
            olimpbet.save_day(conn, day, items, pages, lost)
            total_events += len(items)
            total_pages += pages
            i += 1
            elapsed = time.monotonic() - started
            left = elapsed / i * (len(todo) - i)
            singles = sum(1 for x in items if "Doubles" not in x["tournament"]["name"])
            note = f"  ПРОПУЩЕНО часов: {lost}" if lost else ""
            print(f"[{i:4d}/{len(todo)}] {day}  матчей {len(items):4d} (одиночки {singles:4d}, запросов {pages})  "
                  f"всего {total_events:_}  осталось ~{fmt_eta(left)}{note}".replace("_", " "), flush=True)
    except KeyboardInterrupt:
        print("\nПрервано — повторный запуск продолжит.", flush=True)
    finally:
        http.close()
        n_t, n_e, n_lost = conn.execute(
            """SELECT (SELECT COUNT(*) FROM ob_tournaments), (SELECT COUNT(*) FROM ob_events),
                      (SELECT COALESCE(SUM(failed_hours), 0) FROM ob_days)""").fetchone()
        print(f"в базе: турниров {n_t}, матчей {n_e}, пропущенных часов {n_lost}", flush=True)


class _Paced:
    """HttpClient wrapper: randomized pause before every request."""

    def __init__(self, http, delay: float) -> None:
        self.http, self.delay = http, delay

    def get(self, url: str, params: dict | None = None):
        time.sleep(self.delay * random.uniform(0.7, 1.3))
        return self.http.get(url, params)


if __name__ == "__main__":
    main()
