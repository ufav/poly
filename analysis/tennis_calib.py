"""Tennis pre-match calibration and underdog ROI by surface and tournament tier (A-46).

Favorite price at scheduled start vs win rate; underdog (price <= 0.50) ROI after taker fee.
Uses tennis_links for surface / Olimpbet level. Moneyline markets only.

  python analysis/tennis_calib.py [--min-volume 1000]
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

PRICE_BUCKETS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.98)]
ODDS_BUCKETS = [(2.0, 2.5), (2.5, 3.33), (3.33, 5.0), (5.0, 100.0)]
FEE = 0.05


def series_of(raw: str | None) -> str:
    try:
        return (json.loads(raw) if isinstance(raw, str) else raw or {}).get("seriesSlug") or "?"
    except (TypeError, json.JSONDecodeError):
        return "?"


def tier_of(level: str | None, series: str | None = None) -> str:
    """Tournament significance from Olimpbet level (and PM series as fallback)."""
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


def print_calib(title: str, rows: list[tuple[float, bool]], min_n: int = 30) -> None:
    """rows: (favorite_price, won)."""
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
        print(f"  {'all fav':>11} {n:6d} {avg:7.3f} {win:7.1%} {win - avg:+7.3f} {se2(win, n):6.3f}")
    print()


def print_underdog(title: str, rows: list[tuple[float, bool]], min_n: int = 30) -> None:
    """rows: (underdog_price, won) — price <= 0.50."""
    print(title)
    print(f"  {'odds':>11} {'n':>6} {'win%':>7} {'impl':>7} {'edge':>7} {'ROI':>7} {'±2se':>6}")
    for lo, hi in ODDS_BUCKETS + [(2.0, None)]:
        if hi is None:
            cell = list(rows)
            label = "all >= 2.0"
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
    ap.add_argument("--min-volume", type=float, default=1000)
    ap.add_argument("--min-n", type=int, default=40)
    args = ap.parse_args()

    conn = storage.connect(storage.db_path("tennis"))
    print("indexing pm_prices (once)…", flush=True)
    conn.execute("CREATE INDEX IF NOT EXISTS pm_prices_token_t ON pm_prices(token_id, t)")
    conn.commit()

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    markets = conn.execute(
        """SELECT e.id, e.start_time, e.raw, e.volume, e.score,
                  m.token_a, m.final_price_a,
                  l.surface, l.level, l.ob_event_id
           FROM pm_markets m
           JOIN pm_events e ON e.id = m.event_id
           LEFT JOIN tennis_links l ON l.pm_event_id = e.id
           WHERE m.market_type = 'moneyline' AND m.history_status = 'done'
             AND m.closed = 1 AND m.final_price_a IN (0, 1)
             AND m.volume >= ? AND e.start_time < ?""",
        (args.min_volume, now),
    ).fetchall()
    print(f"markets: {len(markets):,} (volume >= ${args.min_volume:,.0f})\n", flush=True)

    # (fav_p, won, surface, tier, series, dog_p, dog_won)
    samples = []
    skipped = 0
    for _eid, start_time, raw, _vol, _score, token, final_a, surface, level, _ob in markets:
        start = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
        pre = conn.execute(
            "SELECT p FROM pm_prices WHERE token_id = ? AND t <= ? ORDER BY t DESC LIMIT 1",
            (token, start),
        ).fetchone()
        if not pre or not 0.02 < pre[0] < 0.98:
            skipped += 1
            continue
        p_a = pre[0]
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

    print(f"with pre-match price: {len(samples):,}  skipped: {skipped:,}\n", flush=True)

    # --- overall ---
    print_calib("ALL: favorite at scheduled start", [(p, w) for p, w, *_ in samples], args.min_n)

    # --- by series ---
    for ser in ("atp", "wta", "itf"):
        rows = [(p, w) for p, w, _s, _t, series, *_ in samples if series == ser]
        if rows:
            print_calib(f"SERIES {ser.upper()}", rows, args.min_n)

    # --- by surface ---
    for surf in ("Hard", "Clay", "Grass", "Carpet"):
        rows = [(p, w) for p, w, s, *_ in samples if s == surf]
        if rows:
            print_calib(f"SURFACE {surf}", rows, args.min_n)

    # --- by tier ---
    tier_order = ("slam", "finals", "masters", "500", "250/125", "challenger", "itf", "utr", "other_tour", "other")
    for tier in tier_order:
        rows = [(p, w) for p, w, _s, t, *_ in samples if t == tier]
        if rows:
            print_calib(f"TIER {tier}", rows, args.min_n)

    # --- surface × tier (big cells) ---
    print("SURFACE × TIER (all favorites, n>=min)")
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

    # strong favorites (>=0.70) by surface and tier
    print("CLEAR FAVORITES (p>=0.70) by surface / tier")
    print(f"  {'group':28} {'n':>6} {'avg p':>7} {'win%':>7} {'edge':>7} {'±2se':>6}")
    for key_name, pred in (
        *[ (f"surf {s}", lambda p, w, surf, tier, series, dog_p, dog_won, s=s: surf == s)
           for s in ("Hard", "Clay", "Grass") ],
        *[ (f"tier {t}", lambda p, w, surf, tier, series, dog_p, dog_won, t=t: tier == t)
           for t in tier_order ],
    ):
        rows = [(p, w) for p, w, surf, tier, series, dog_p, dog_won in samples
                if p >= 0.70 and pred(p, w, surf, tier, series, dog_p, dog_won)]
        if len(rows) < args.min_n:
            continue
        n = len(rows)
        avg = sum(x for x, _ in rows) / n
        win = sum(w for _, w in rows) / n
        print(f"  {key_name:28} {n:6d} {avg:7.3f} {win:7.1%} {win - avg:+7.3f} {se2(win, n):6.3f}")
    print()

    # --- underdogs ---
    dogs = [(dog_p, dog_won, surf, tier, series)
            for fav_p, fav_won, surf, tier, series, dog_p, dog_won in samples]
    print_underdog("UNDERDOG all (price<=0.50 at start), ROI after taker fee",
                   [(p, w) for p, w, *_ in dogs], args.min_n)
    for surf in ("Hard", "Clay", "Grass"):
        rows = [(p, w) for p, w, s, *_ in dogs if s == surf]
        if rows:
            print_underdog(f"UNDERDOG surface {surf}", rows, args.min_n)
    for tier in ("slam", "masters", "500", "250/125", "challenger", "itf"):
        rows = [(p, w) for p, w, _s, t, *_ in dogs if t == tier]
        if rows:
            print_underdog(f"UNDERDOG tier {tier}", rows, args.min_n)

    # underdog 2.0-2.5 highlight by surface × tier
    print("UNDERDOG odds 2.0–2.5 by surface × tier (ROI after fee)")
    print(f"  {'group':28} {'n':>6} {'win%':>7} {'impl':>7} {'edge':>7} {'ROI':>7} {'±2se':>6}")
    for surf in ("Hard", "Clay", "Grass", "all"):
        for tier in ("slam", "masters", "500", "250/125", "challenger", "itf", "all"):
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
            label = f"{surf} / {tier}"
            print(f"  {label:28} {n:6d} {win:7.1%} {avg:7.1%} {win - avg:+7.1%} {roi:+7.1%} {se2(win, n):6.3f}")
    print()


if __name__ == "__main__":
    main()
