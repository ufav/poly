"""Estimate when a map started, ended and reached halftime from its Polymarket price path.

No round timestamps exist in the data, so:
- map end   = first point of the final settled run (price of the winner >= 0.95 until the end of data);
- map start = first move >= START_MOVE between consecutive points after `lower` (previous map end or
              scheduled start); the pistol round moves a map price by 3-10 cents;
- round k   = start + (end - start) * k / rounds  (rounds take roughly equal time, ~2 min).
"""

import sqlite3

START_MOVE = 0.03
SETTLED = 0.95


def price_series(conn: sqlite3.Connection, token: str) -> list[tuple[int, float]]:
    return conn.execute("SELECT t, p FROM pm_prices WHERE token_id = ? ORDER BY t", (token,)).fetchall()


def map_window(series: list[tuple[int, float]], lower: int, a_won: bool) -> tuple[int, int] | None:
    """(start, end) unix seconds of the map for the outcome-A token series, or None."""
    pts = [(t, p if a_won else 1 - p) for t, p in series if t >= lower]
    if len(pts) < 10 or pts[-1][1] < SETTLED:
        return None
    end_idx = len(pts) - 1
    while end_idx > 0 and pts[end_idx - 1][1] >= SETTLED:
        end_idx -= 1
    end = pts[end_idx][0]
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if t1 >= end:
            return None
        if abs(p1 - p0) >= START_MOVE:
            return t0, end
    return None


def price_at(series: list[tuple[int, float]], t: int) -> float | None:
    """Last price at or before t."""
    last = None
    for ts, p in series:
        if ts > t:
            break
        last = p
    return last
