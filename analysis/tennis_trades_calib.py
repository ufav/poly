"""Tennis pre-match calibration on real trades (A-46 / R-21 follow-up).

Executable price = last taker trade before scheduled start (BUY of token_a at p, or SELL at 1-p
as a bet on A). Same surface / tier cuts as tennis_calib.py. ROI after taker fee; ±2se binomial
over matches (one price per match).

  python analysis/tennis_trades_calib.py [--min-volume 50000]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402

FEE = 0.05
PRICE_BUCKETS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.98)]
ODDS_BUCKETS = [(2.0, 2.5), (2.5, 3.33), (3.33, 5.0), (5.0, 100.0)]


def series_of(raw: str | None) -> str:
    try:
        return (json.loads(raw) if isinstance(raw, str) else raw or {}).get("seriesSlug") or "?"
    except (TypeError, json.JSONDecodeError):
        return "?"


def tier_of(level: str | None, series: str | None = None) -> str:
    s = (level or "").lower()
    if any(k in s for k in ("australian open", "roland garros", "wimbledon", "us open", "french open")):
        return "slam"
    if "finals" in s or "laver cup" in s or "next gen" in s:
        return "finals"
    if "1000" in s:
        return "masters"
    if re.search(r"\b500\b", s):
        return "500"
    if re.search(r"\b250\b", s) or "wta 125" in s or re.search(r"\b125\b", s):
        return "250/125"
    if "challenger" in s:
        return "challenger"
    if any(k in s for k in ("world tennis", "itf", "m15", "m25", "w15", "w25", "w35", "w50", "w75", "w100")):
        return "itf"
    if "utr" in s:
        return "utr"
    if series == "itf":
        return "itf"
    if series in ("atp", "wta"):
        return "other_tour"
    return "other"


def fee(p: float) -> float:
    return FEE * p * (1 - p)


def se2(win: float, n: int) -> float:
    return 2 * (win * (1 - win) / n) ** 0.5 if n else 0.0


def last_pre_price(conn, cond: str, tok_a: str, start_ts: int) -> float | None:
    """Implied P(A) from the last trade at or before start."""
    row = conn.execute(
        """SELECT asset, side, price FROM pm_trades
           WHERE condition_id = ? AND t <= ?
           ORDER BY t DESC LIMIT 1""",
        (cond, start_ts),
    ).fetchone()
    if not row:
        return None
    asset, side, price = row
    on_a = (asset == tok_a) == (side == "BUY")
    return price if on_a else 1 - price


def print_calib(title: str, rows: list[tuple[float, bool]], min_n: int = 40) -> None:
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
    if len(rows) >= min_n:
        n = len(rows)
        avg = sum(p for p, _ in rows) / n
        win = sum(w for _, w in rows) / n
        print(f"  {'all fav':>11} {n:6d} {avg:7.3f} {win:7.1%} {win - avg:+7.3f} {se2(win, n):6.3f}")
    print()


def print_underdog(title: str, rows: list[tuple[float, bool]], min_n: int = 40) -> None:
    print(title)
    print(f"  {'odds':>11} {'n':>6} {'win%':>7} {'impl':>7} {'edge':>7} {'ROI':>7} {'±2se':>6}")
    for lo, hi in ODDS_BUCKETS + [(2.0, None)]:
        if hi is None:
            cell, label = list(rows), "all >= 2.0"
        else:
            cell = [(p, w) for p, w in rows if lo <= 1 / p < hi]
            label = f"{lo:.2f}-{hi:.2f}" if hi < 100 else f"{lo:.2f}+"
        if len(cell) < min_n:
            continue
        n = len(cell)
        avg = sum(p for p, _ in cell) / n
        win = sum(w for _, w in cell) / n
        roi = sum(((1 if w else 0) / (p + fee(p)) - 1) for p, w in cell) / n
        print(f"  {label:>11} {n:6d} {win:7.1%} {avg:7.1%} {win - avg:+7.1%} {roi:+7.1%} {se2(win, n):6.3f}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-volume", type=float, default=50_000)
    ap.add_argument("--min-n", type=int, default=40)
    args = ap.parse_args()

    conn = storage.connect(storage.db_path("tennis"))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    markets = conn.execute(
        """SELECT e.id, e.start_time, e.raw, m.condition_id, m.token_a, m.final_price_a,
                  l.surface, l.level
           FROM pm_markets m
           JOIN pm_events e ON e.id = m.event_id
           LEFT JOIN tennis_links l ON l.pm_event_id = e.id
           WHERE m.market_type = 'moneyline' AND m.trades_status = 'done'
             AND m.final_price_a IN (0, 1) AND m.volume >= ?
             AND e.start_time < ?""",
        (args.min_volume, now),
    ).fetchall()
    print(f"markets with trades: {len(markets):,} (volume >= ${args.min_volume:,.0f})", flush=True)

    samples = []  # fav_p, fav_won, surface, tier, series, dog_p, dog_won
    skipped = 0
    for i, (_eid, start_time, raw, cond, tok_a, final_a, surface, level) in enumerate(markets):
        if i and i % 1000 == 0:
            print(f"  … {i}/{len(markets)}", flush=True)
        try:
            start_ts = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
        except (AttributeError, ValueError):
            skipped += 1
            continue
        p_a = last_pre_price(conn, cond, tok_a, start_ts)
        if p_a is None or not 0.02 < p_a < 0.98:
            skipped += 1
            continue
        series = series_of(raw)
        surface = surface or "?"
        tier = tier_of(level, series)
        if p_a >= 0.5:
            fav_p, fav_won = p_a, final_a == 1
            dog_p, dog_won = 1 - p_a, final_a == 0
        else:
            fav_p, fav_won = 1 - p_a, final_a == 0
            dog_p, dog_won = p_a, final_a == 1
        samples.append((fav_p, fav_won, surface, tier, series, dog_p, dog_won))

    print(f"with last pre-match trade: {len(samples):,}  skipped: {skipped:,}\n", flush=True)

    print_calib("ALL (last trade before start)", [(p, w) for p, w, *_ in samples], args.min_n)
    for ser in ("atp", "wta", "itf"):
        rows = [(p, w) for p, w, _s, _t, series, *_ in samples if series == ser]
        if rows:
            print_calib(f"SERIES {ser.upper()}", rows, args.min_n)
    for surf in ("Hard", "Clay", "Grass"):
        rows = [(p, w) for p, w, s, *_ in samples if s == surf]
        if rows:
            print_calib(f"SURFACE {surf}", rows, args.min_n)
    for tier in ("slam", "masters", "500", "250/125", "challenger", "itf"):
        rows = [(p, w) for p, w, _s, t, *_ in samples if t == tier]
        if rows:
            print_calib(f"TIER {tier}", rows, args.min_n)

    print("SURFACE × TIER")
    print(f"  {'group':28} {'n':>6} {'avg p':>7} {'win%':>7} {'edge':>7} {'±2se':>6}")
    for surf in ("Hard", "Clay", "Grass"):
        for tier in ("slam", "masters", "500", "250/125", "challenger", "itf"):
            rows = [(p, w) for p, w, s, t, *_ in samples if s == surf and t == tier]
            if len(rows) < args.min_n:
                continue
            n = len(rows)
            avg = sum(p for p, _ in rows) / n
            win = sum(w for _, w in rows) / n
            print(f"  {f'{surf} / {tier}':28} {n:6d} {avg:7.3f} {win:7.1%} {win - avg:+7.3f} {se2(win, n):6.3f}")
    print()

    dogs = [(dog_p, dog_won, surf, tier, series)
            for fav_p, fav_won, surf, tier, series, dog_p, dog_won in samples]
    print_underdog("UNDERDOG (last trade), ROI after fee", [(p, w) for p, w, *_ in dogs], args.min_n)
    for tier in ("slam", "masters", "challenger", "itf"):
        rows = [(p, w) for p, w, _s, t, *_ in dogs if t == tier]
        if rows:
            print_underdog(f"UNDERDOG tier {tier}", rows, args.min_n)

    print("UNDERDOG odds 2.0–2.5 by surface × tier")
    print(f"  {'group':28} {'n':>6} {'win%':>7} {'impl':>7} {'edge':>7} {'ROI':>7} {'±2se':>6}")
    for surf in ("Hard", "Clay", "all"):
        for tier in ("slam", "masters", "challenger", "itf", "all"):
            rows = [(p, w) for p, w, s, t, *_ in dogs
                    if 2.0 <= 1 / p < 2.5
                    and (surf == "all" or s == surf)
                    and (tier == "all" or t == tier)]
            if len(rows) < args.min_n:
                continue
            n = len(rows)
            avg = sum(p for p, _ in rows) / n
            win = sum(w for _, w in rows) / n
            roi = sum(((1 if w else 0) / (p + fee(p)) - 1) for p, w in rows) / n
            print(f"  {f'{surf} / {tier}':28} {n:6d} {win:7.1%} {avg:7.1%} {win - avg:+7.1%} {roi:+7.1%} {se2(win, n):6.3f}")
    print()

if __name__ == "__main__":
    main()
