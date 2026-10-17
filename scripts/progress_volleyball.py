"""Live progress of volleyball Polymarket collection (data/volleyball.sqlite).

  python -u scripts/progress_volleyball.py
  python -u scripts/progress_volleyball.py --once
"""

import argparse
import datetime as dt
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot.storage import db_path  # noqa: E402


def bar(done: float, total: float, width: int = 40) -> str:
    frac = done / total if total else 0
    filled = int(frac * width)
    return f"[{'#' * filled}{'.' * (width - filled)}] {frac * 100:5.1f}%"


def snap(conn: sqlite3.Connection) -> dict:
    e, m, done, empty, pending, pts, vol = conn.execute(
        """SELECT
            (SELECT COUNT(*) FROM pm_events),
            (SELECT COUNT(*) FROM pm_markets),
            (SELECT COUNT(*) FROM pm_markets WHERE history_status='done'),
            (SELECT COUNT(*) FROM pm_markets WHERE history_status='empty'),
            (SELECT COUNT(*) FROM pm_markets WHERE history_status='pending'),
            (SELECT COUNT(*) FROM pm_prices),
            (SELECT COALESCE(SUM(volume),0) FROM pm_events)
        """
    ).fetchone()
    trades_done = conn.execute(
        "SELECT COUNT(*) FROM pm_markets WHERE trades_status='done'"
    ).fetchone()[0]
    try:
        ob_days, ob_events, ob_last = conn.execute(
            """SELECT COUNT(*),
                      (SELECT COUNT(*) FROM ob_events),
                      (SELECT MAX(day) FROM ob_days)"""
        ).fetchone()
    except sqlite3.OperationalError:
        ob_days, ob_events, ob_last = 0, 0, None
    return {"t": time.monotonic(), "events": e or 0, "markets": m or 0, "done": done or 0,
            "empty": empty or 0, "pending": pending or 0, "pts": pts or 0, "vol": vol or 0,
            "trades_done": trades_done or 0, "ob_days": ob_days or 0, "ob_events": ob_events or 0,
            "ob_last": ob_last}


def render(s: dict, first: dict) -> str:
    minutes = (s["t"] - first["t"]) / 60
    hist_total = s["done"] + s["empty"] + s["pending"]
    rate = (s["done"] + s["empty"] - first["done"] - first["empty"]) / minutes if minutes > 0.1 else None
    left = s["pending"]
    eta = "—"
    if rate and rate > 0 and left:
        m = left / rate
        eta = f"~{int(m // 60)} ч {int(m % 60):02d} мин" if m >= 60 else f"~{int(m)} мин"
    now = dt.datetime.now().strftime("%H:%M:%S")
    return "\n".join([
        f"Волейбол — прогресс сбора                         {now}",
        f"События: {s['events']}   рынки: {s['markets']}   объём событий: ${s['vol']:,.0f}",
        "",
        "Истории цен (moneyline / others)",
        f"  {bar(s['done'] + s['empty'], hist_total)}",
        f"  done {s['done']}  empty {s['empty']}  pending {s['pending']}  points {s['pts']:,}",
        f"  скорость ~{rate:.1f}/мин  ETA {eta}" if rate else "  скорость —",
        "",
        f"Сделки (trades_status=done): {s['trades_done']}",
        "",
        f"Olimpbet (sport 104): дней {s['ob_days']}  матчей {s['ob_events']}  последний день {s['ob_last'] or '—'}",
    ])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    path = db_path("volleyball")
    if not path.exists():
        print(f"нет базы {path} — сначала: scripts\\collect_volleyball.bat")
        return
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    first = snap(conn)
    try:
        while True:
            s = snap(conn)
            print("\033[H\033[J" + render(s, first), flush=True)
            if args.once:
                return
            time.sleep(10)
    except KeyboardInterrupt:
        print()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
