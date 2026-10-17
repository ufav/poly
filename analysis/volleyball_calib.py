"""Volleyball pre-match calibration on Polymarket moneyline (A-48 / R-25).

No set scores on Gamma for volleyball yet — only favorite price at start vs outcome,
by series (VNL / Euro / domestic). Thin market: most events < $1k.

  python analysis/volleyball_calib.py [--min-volume 0]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402
from pmbot.maptiming import price_at, price_series  # noqa: E402

PRICE_BUCKETS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.98)]
ODDS_BUCKETS = [(2.0, 2.5), (2.5, 3.33), (3.33, 5.0), (5.0, 100.0)]
FEE = 0.05

# short labels for seriesSlug (vbvnl-games → vnl)
SERIES_LABEL = {
    "vbvnl-games": "vnl",
    "vbeuro-games": "euro",
    "vbplusliga-games": "plusliga",
    "vbsuperlega-games": "superlega",
    "vbliguea-games": "liguea",
    "vbligueaw-games": "liguea-w",
    "vbligueb-games": "ligueb",
    "vbbundesliga-games": "bundesliga",
    "vbbundesligaw-games": "bundesliga-w",
    "vb2bundesliga-games": "2.bundesliga",
    "vbcl-games": "champions",
    "vbworlds-games": "worlds",
    "vbsuperliga-games": "superliga",
    "vbefeler-games": "efeler",
    "vbsvleague-games": "sv.league",
    "vbvleague-games": "v.league",
    "vbbelligaa-games": "beliga",
    "vbdivisao1-games": "divisao1",
}


def series_of(raw: str | None) -> str:
    try:
        slug = (json.loads(raw) if isinstance(raw, str) else raw or {}).get("seriesSlug") or "?"
    except (TypeError, json.JSONDecodeError):
        return "?"
    return SERIES_LABEL.get(slug, slug.replace("-games", "") if slug.endswith("-games") else slug)


def fee(p: float) -> float:
    return FEE * p * (1 - p)


def se2(win: float, n: int) -> float:
    return 2 * (win * (1 - win) / n) ** 0.5 if n else 0.0


def print_calib(title: str, rows: list[tuple[float, bool]], min_n: int = 15) -> None:
    print(title)
    print(f"  {'bucket':>11} {'n':>6} {'avg p':>7} {'win%':>7} {'edge':>7} {'±2se':>6}")
    for lo, hi in PRICE_BUCKETS:
        cell = [(p, w) for p, w in rows if lo <= p < hi]
        if len(cell) < min_n:
            continue
        n = len(cell)
        avg = sum(p for p, _ in cell) / n
        win = sum(w for _, w in cell) / n
        print(f"  {lo:.2f}-{hi:.2f} {n:6d} {avg:7.3f} {win:7.1%} {win - avg:+7.3f} {se2(win, n):6.3f}")
    if rows:
        n = len(rows)
        avg = sum(p for p, _ in rows) / n
        win = sum(w for _, w in rows) / n
        print(f"  {'all':>11} {n:6d} {avg:7.3f} {win:7.1%} {win - avg:+7.3f} {se2(win, n):6.3f}")


def print_dog_roi(title: str, dogs: list[tuple[float, bool]], min_n: int = 15) -> None:
    """dogs: (underdog_price, underdog_won)."""
    print(title)
    print(f"  {'odds':>11} {'n':>6} {'avg p':>7} {'win%':>7} {'ROI':>8} {'±2se':>6}")
    for lo, hi in ODDS_BUCKETS:
        cell = [(p, w) for p, w in dogs if lo <= 1 / p < hi]
        if len(cell) < min_n:
            continue
        n = len(cell)
        avg = sum(p for p, _ in cell) / n
        win = sum(w for _, w in cell) / n
        roi = sum(((1 if w else 0) / (p + fee(p)) - 1) for p, w in cell) / n
        print(f"  {lo:.2f}-{hi:.2g} {n:6d} {avg:7.3f} {win:7.1%} {100 * roi:+7.1f}% {se2(win, n):6.3f}")
    if dogs:
        n = len(dogs)
        avg = sum(p for p, _ in dogs) / n
        win = sum(w for _, w in dogs) / n
        roi = sum(((1 if w else 0) / (p + fee(p)) - 1) for p, w in dogs) / n
        print(f"  {'all dogs':>11} {n:6d} {avg:7.3f} {win:7.1%} {100 * roi:+7.1f}% {se2(win, n):6.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-volume", type=float, default=0)
    ap.add_argument("--min-n", type=int, default=15)
    args = ap.parse_args()

    conn = storage.connect(storage.db_path("volleyball"))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = conn.execute(
        """SELECT e.start_time, e.raw, e.volume, m.token_a, m.final_price_a, m.volume
           FROM pm_markets m
           JOIN pm_events e ON e.id = m.event_id
           WHERE m.market_type = 'moneyline' AND m.history_status = 'done'
             AND m.closed = 1 AND m.final_price_a IN (0, 1)
             AND m.volume >= ? AND e.start_time < ?""",
        (args.min_volume, now),
    ).fetchall()
    print(f"moneyline with history: {len(rows):,} (vol >= ${args.min_volume:,.0f})", flush=True)

    by_all: list[tuple[float, bool]] = []
    by_ser: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    dogs_all: list[tuple[float, bool]] = []
    dogs_ser: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    skipped = 0

    for i, (start_time, raw, _ev_vol, token, final_a, _m_vol) in enumerate(rows):
        if i and i % 100 == 0:
            print(f"  … {i}/{len(rows)}", flush=True)
        start = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
        series_a = price_series(conn, token)
        p_a0 = price_at(series_a, start)
        if p_a0 is None or not 0.02 < p_a0 < 0.98:
            skipped += 1
            continue
        fav_is_a = p_a0 >= 0.5
        fav_p = p_a0 if fav_is_a else 1 - p_a0
        fav_won = (final_a == 1) if fav_is_a else (final_a == 0)
        dog_p = 1 - fav_p
        ser = series_of(raw)
        by_all.append((fav_p, fav_won))
        by_ser[ser].append((fav_p, fav_won))
        dogs_all.append((dog_p, not fav_won))
        dogs_ser[ser].append((dog_p, not fav_won))

    print(f"used {len(by_all)}, skipped price {skipped}\n")
    print_calib("ALL", by_all, args.min_n)
    print_dog_roi("\nUNDERDOG ROI (taker fee)", dogs_all, args.min_n)
    for ser in sorted(by_ser, key=lambda s: -len(by_ser[s])):
        print()
        print_calib(f"SERIES {ser} (n={len(by_ser[ser])})", by_ser[ser], max(10, args.min_n // 2))
        print_dog_roi(f"  dogs {ser}", dogs_ser[ser], max(10, args.min_n // 2))
    print()


if __name__ == "__main__":
    main()
