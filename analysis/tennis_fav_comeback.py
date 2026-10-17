"""When does the pre-match favorite come back after losing set 1? (A-47)

Universe: moneyline matches where favorite lost set 1 (vol ≥ --min-volume).
Rate = P(fav wins match | lost S1). Cuts: set-1 score (fav view), score class,
pre-match odds, surface, tour, tier; score × odds; also set-2 win rate and path LWW/LWL.

  python analysis/tennis_fav_comeback.py [--min-volume 5000]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402
from pmbot.maptiming import price_at, price_series  # noqa: E402

from analysis.tennis_sets import (  # noqa: E402
    FAV_BANDS,
    SERIES_ORDER,
    SURF_ORDER,
    TIER_ORDER,
    band_fav,
    fav_set_wins,
    parse_sets_ob,
    parse_sets_pm,
    se2,
    series_of,
    set_complete,
    tier_of,
)

# set-1 score from favorite's perspective after a loss (games-games)
SCORE_ORDER = ("0-6", "1-6", "2-6", "3-6", "4-6", "5-7", "6-7", "other")
# closeness of the lost set
CLASS_ORDER = ("blowout 0–1", "mid 2–3", "close 4–6", "tb 6-7/5-7")


def s1_fav_view(sets_ab: list[tuple[int, int]], fav_is_a: bool) -> tuple[int, int] | None:
    a, b = sets_ab[0]
    if not set_complete(a, b):
        return None
    return (a, b) if fav_is_a else (b, a)


def score_label(fg: int, dg: int) -> str:
    key = f"{fg}-{dg}"
    return key if key in SCORE_ORDER[:-1] else "other"


def score_class(fg: int, dg: int) -> str:
    if (fg, dg) == (6, 7) or (fg, dg) == (5, 7):
        return "tb 6-7/5-7"
    if fg <= 1:
        return "blowout 0–1"
    if fg <= 3:
        return "mid 2–3"
    return "close 4–6"


class Agg:
    __slots__ = ("n", "match", "s2", "lww", "lwl", "ll")

    def __init__(self) -> None:
        self.n = self.match = self.s2 = self.lww = self.lwl = self.ll = 0

    def add(self, fav_won: bool, wins: list[bool]) -> None:
        self.n += 1
        self.match += int(fav_won)
        if len(wins) >= 2 and wins[1]:
            self.s2 += 1
        path = "".join("W" if x else "L" for x in wins)
        if path.startswith("LWW"):
            self.lww += 1
        elif path.startswith("LWL"):
            self.lwl += 1
        elif path.startswith("LL"):
            self.ll += 1


def print_table(title: str, cells: dict[str, Agg], order: list[str], min_n: int = 40) -> None:
    print(f"\n=== {title} ===")
    print(f"{'group':28} {'n':>6} {'come%':>7} {'±2se':>6} {'S2%':>7} {'LWW%':>7} {'LWL%':>7} {'LL%':>7}")
    for key in order:
        a = cells.get(key)
        if not a or a.n < min_n:
            continue
        n = a.n
        come = a.match / n
        print(f"{key:28} {n:6d} {100 * come:6.1f}% {se2(come, n):6.3f} "
              f"{100 * a.s2 / n:6.1f}% {100 * a.lww / n:6.1f}% {100 * a.lwl / n:6.1f}% "
              f"{100 * a.ll / n:6.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-volume", type=float, default=5000)
    ap.add_argument("--min-n", type=int, default=40)
    args = ap.parse_args()

    conn = storage.connect(storage.db_path("tennis"))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = conn.execute(
        """SELECT e.start_time, e.raw, e.score, m.token_a, m.final_price_a,
                  l.surface, l.level, l.side_flip, ob.set_scores
           FROM pm_markets m
           JOIN pm_events e ON e.id = m.event_id
           JOIN tennis_links l ON l.pm_event_id = e.id
           LEFT JOIN ob_events ob ON ob.id = l.ob_event_id
           WHERE m.market_type = 'moneyline' AND m.history_status = 'done'
             AND m.closed = 1 AND m.final_price_a IN (0, 1)
             AND m.volume >= ? AND e.start_time < ?
             AND l.ob_event_id IS NOT NULL""",
        (args.min_volume, now),
    ).fetchall()
    print(f"candidates: {len(rows):,} (vol >= ${args.min_volume:,.0f})", flush=True)

    cells: dict[str, dict[str, Agg]] = defaultdict(lambda: defaultdict(Agg))
    skipped = defaultdict(int)

    for i, (start_time, raw, score, token, final_a, surface, level, side_flip, ob_scores) in enumerate(rows):
        if i and i % 2000 == 0:
            print(f"  … {i}/{len(rows)}", flush=True)
        start = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
        series_a = price_series(conn, token)
        p_a0 = price_at(series_a, start)
        if p_a0 is None or not 0.02 < p_a0 < 0.98:
            skipped["price"] += 1
            continue
        fav_is_a = p_a0 >= 0.5
        fav_p0 = p_a0 if fav_is_a else 1 - p_a0
        fav_won = (final_a == 1) if fav_is_a else (final_a == 0)

        sets_ab = parse_sets_pm(score) or parse_sets_ob(ob_scores, side_flip)
        if not sets_ab:
            skipped["sets"] += 1
            continue
        wins = fav_set_wins(sets_ab, fav_is_a)
        if not wins:
            skipped["sets"] += 1
            continue
        sets_fav = sum(wins)
        sets_dog = len(wins) - sets_fav
        if sets_fav == sets_dog or (sets_fav > sets_dog) != fav_won:
            skipped["mismatch"] += 1
            continue
        if wins[0]:
            skipped["fav_won_s1"] += 1
            continue  # only lost-S1 universe

        s1 = s1_fav_view(sets_ab, fav_is_a)
        if not s1:
            skipped["sets"] += 1
            continue
        fg, dg = s1
        if fg >= dg:
            skipped["s1_side"] += 1
            continue

        fb = band_fav(fav_p0) or "?"
        surf = surface or "?"
        tour = series_of(raw)
        tier = tier_of(level, tour)
        sc = score_label(fg, dg)
        cls = score_class(fg, dg)

        tags = {
            "all": ["all"],
            "score": [sc],
            "class": [cls],
            "fav odds": [f"fav {fb}"],
            "surface": [f"surf {surf}"],
            "tour": [f"tour {tour}"],
            "tier": [f"tier {tier}"],
            "score × odds": [f"{sc} × {fb}"],
            "class × odds": [f"{cls} × {fb}"],
            "class × surface": [f"{cls} × {surf}"],
            "class × tour": [f"{cls} × {tour}"],
            "odds × surface": [f"fav {fb} × {surf}"],
            "odds × tour": [f"fav {fb} × {tour}"],
        }
        for cut, keys in tags.items():
            for key in keys:
                cells[cut][key].add(fav_won, wins)

    print(f"skipped: {dict(skipped)}", flush=True)
    print("Universe: favorite lost set 1. come% = fav wins match; S2% = fav wins set 2; "
          "LWW/LWL/LL = path shares.\n")

    orders = {
        "all": ["all"],
        "score": list(SCORE_ORDER),
        "class": list(CLASS_ORDER),
        "fav odds": [f"fav {b[2]}" for b in FAV_BANDS],
        "surface": [f"surf {s}" for s in SURF_ORDER],
        "tour": [f"tour {s}" for s in SERIES_ORDER],
        "tier": [f"tier {t}" for t in TIER_ORDER],
        "score × odds": [f"{sc} × {b[2]}" for sc in SCORE_ORDER[:-1] for b in FAV_BANDS],
        "class × odds": [f"{c} × {b[2]}" for c in CLASS_ORDER for b in FAV_BANDS],
        "class × surface": [f"{c} × {s}" for c in CLASS_ORDER for s in SURF_ORDER],
        "class × tour": [f"{c} × {s}" for c in CLASS_ORDER for s in SERIES_ORDER],
        "odds × surface": [f"fav {b[2]} × {s}" for b in FAV_BANDS for s in SURF_ORDER],
        "odds × tour": [f"fav {b[2]} × {s}" for b in FAV_BANDS for s in SERIES_ORDER],
    }
    for cut, order in orders.items():
        print_table(cut, cells[cut], order, args.min_n)

    # headline ranking by come% among score × strong-fav
    print("\n=== highest comeback pockets (n≥80) ===")
    ranked = []
    for cut in ("score", "class", "score × odds", "class × odds", "class × surface", "class × tour"):
        for key, a in cells[cut].items():
            if a.n >= 80:
                ranked.append((a.match / a.n, a.n, cut, key))
    ranked.sort(reverse=True)
    print(f"{'come%':>7} {'n':>5} {'cut':18} group")
    for rate, n, cut, key in ranked[:25]:
        print(f"{100 * rate:6.1f}% {n:5d} {cut:18} {key}")
    print("\n=== lowest comeback pockets (n≥80) ===")
    for rate, n, cut, key in ranked[-15:]:
        print(f"{100 * rate:6.1f}% {n:5d} {cut:18} {key}")
    print()


if __name__ == "__main__":
    main()
