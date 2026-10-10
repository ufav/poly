"""Live progress of both collectors (read-only DB access).

  python scripts/progress.py            # refresh every 10 s, Ctrl+C to exit
  python scripts/progress.py --once
"""

import argparse
import datetime as dt
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot.storage import DEFAULT_DB  # noqa: E402

CS_SINCE = dt.date(2025, 9, 1)


def bar(done: float, total: float, width: int = 40) -> str:
    frac = done / total if total else 0
    filled = int(frac * width)
    return f"[{'#' * filled}{'.' * (width - filled)}] {frac * 100:5.1f}%"


def eta(remaining: float, rate_per_min: float | None) -> str:
    if not rate_per_min or rate_per_min <= 0:
        return "—"
    minutes = remaining / rate_per_min
    return f"~{int(minutes // 60)} ч {int(minutes % 60)} мин" if minutes >= 60 else f"~{int(minutes)} мин"


def snapshot(conn: sqlite3.Connection) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()
    pm = q("""SELECT SUM(history_status IN ('done','empty')), SUM(history_status <> 'skipped'),
                     (SELECT COUNT(*) FROM pm_events) FROM pm_markets""")
    cs = q("""SELECT COUNT(*), SUM(stats_status = 'done'),
                     (SELECT date FROM cs_matches ORDER BY fetched_at DESC LIMIT 1),
                     (SELECT COUNT(*) FROM cs_rounds) FROM cs_matches WHERE date >= '2025-09-01'""")
    return {"t": time.monotonic(), "pm_done": pm[0] or 0, "pm_total": pm[1] or 0, "pm_events": pm[2] or 0,
            "cs_matches": cs[0] or 0, "cs_rounds_matches": cs[1] or 0, "cs_last_date": cs[2],
            "cs_rounds": cs[3] or 0}


def render(s: dict, first: dict) -> str:
    minutes = (s["t"] - first["t"]) / 60
    pm_rate = (s["pm_done"] - first["pm_done"]) / minutes if minutes > 0.1 else None
    days_total = (dt.date.today() - CS_SINCE).days + 1
    days_done = (dt.date.fromisoformat(s["cs_last_date"]) - CS_SINCE).days + 1 if s["cs_last_date"] else 0
    first_days = (dt.date.fromisoformat(first["cs_last_date"]) - CS_SINCE).days + 1 if first["cs_last_date"] else 0
    cs_rate = (days_done - first_days) / minutes if minutes > 0.1 else None
    share = s["cs_rounds_matches"] / s["cs_matches"] * 100 if s["cs_matches"] else 0
    now = dt.datetime.now().strftime("%H:%M:%S")
    return "\n".join([
        f"Прогресс сбора данных CS2                       {now}",
        "",
        "Polymarket — история цен (матч + карты)",
        f"  {bar(s['pm_done'], s['pm_total'])}",
        f"  рынков: {s['pm_done']:,} / {s['pm_total']:,}   матчей в базе: {s['pm_events']:,}",
        f"  скорость: {pm_rate:.0f} рынков/мин   осталось: {eta(s['pm_total'] - s['pm_done'], pm_rate)}"
        if pm_rate else "  скорость: считаю…",
        "",
        "cybersport.ru — матчи и раунды",
        f"  {bar(days_done, days_total)}",
        f"  дошёл до: {s['cs_last_date'] or '—'}   дней: {days_done} / {days_total}",
        f"  матчей: {s['cs_matches']:,}   с раундами: {s['cs_rounds_matches']:,} ({share:.0f}%)   раундов: {s['cs_rounds']:,}",
        f"  скорость: {cs_rate:.2f} дня/мин   осталось: {eta(days_total - days_done, cs_rate)}"
        if cs_rate else "  скорость: считаю…",
        "",
        "Ctrl+C — выход",
    ]).replace(",", " ")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=10)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True, timeout=30)
    first = snapshot(conn)
    try:
        while True:
            s = snapshot(conn)
            if not args.once:
                os.system("cls" if os.name == "nt" else "clear")
            print(render(s, first), flush=True)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
