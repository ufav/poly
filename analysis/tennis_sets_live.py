"""ROI after set 1 on tennis moneyline, using price path to time the end of set 1 (A-46 / R-23).

Set-1 end ≈ time of max move (in the set-1 winner's direction) on the favorite's mid-price in
[start+12min, start+90min], then +2 min settle. Strategies (flat $1, taker fee 0.05·p·(1−p)):

  fav_comeback  — favorite lost S1 → buy favorite
  dog_close     — favorite lost S1 → buy underdog
  fav_continue  — favorite won S1  → buy favorite
  dog_upset     — favorite won S1  → buy underdog

Cuts: pre-match favorite odds, underdog odds, surface, tier.

  python analysis/tennis_sets_live.py [--min-volume 5000]
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
    DOG_BANDS,
    FAV_BANDS,
    SERIES_ORDER,
    SURF_ORDER,
    TIER_ORDER,
    band_dog,
    band_fav,
    fav_set_wins,
    parse_sets_ob,
    parse_sets_pm,
    se2,
    series_of,
    tier_of,
)

FEE = 0.05
SET1_LO = 18 * 60
SET1_HI = 50 * 60       # set 1 usually done; beyond ~50 min bleeds into set 2
SETTLE = 120
MIN_MOVE = 0.08          # full set-1 reaction, not a break point blip


def fee(p: float) -> float:
    return FEE * p * (1 - p)


def detect_set1_end(series: list[tuple[int, float]], start: int, fav_won_s1: bool
                    ) -> tuple[int, float, float] | None:
    """Time of max move toward set-1 winner in [start+18m, start+50m]; entry + SETTLE.

    Returns (entry_ts, p_fav_at_entry, move_from_start) or None if move < MIN_MOVE.
    """
    p0 = price_at(series, start)
    if p0 is None or not 0.02 < p0 < 0.98:
        return None
    lo, hi = start + SET1_LO, start + SET1_HI
    best_t, best_move = None, -1.0
    for t, p in series:
        if t < lo:
            continue
        if t > hi:
            break
        move = (p - p0) if fav_won_s1 else (p0 - p)
        if move > best_move:
            best_move, best_t = move, t
    if best_t is None or best_move < MIN_MOVE:
        return None
    entry_t = best_t + SETTLE
    p_entry = price_at(series, entry_t)
    if p_entry is None or not 0.02 < p_entry < 0.98:
        return None
    return entry_t, p_entry, best_move


class Agg:
    __slots__ = ("n", "wins", "sum_p", "sum_ret", "sum_move", "delays")

    def __init__(self) -> None:
        self.n = self.wins = 0
        self.sum_p = self.sum_ret = self.sum_move = 0.0
        self.delays: list[int] = []

    def add(self, p: float, won: bool, move: float, delay: int) -> None:
        self.n += 1
        self.wins += int(won)
        self.sum_p += p
        self.sum_ret += (1 if won else 0) / (p + fee(p)) - 1
        self.sum_move += move
        self.delays.append(delay)


def print_roi(title: str, cells: dict[str, Agg], order: list[str], min_n: int = 40) -> None:
    print(f"\n=== {title} ===")
    print(f"{'group':28} {'n':>6} {'win%':>7} {'avg p':>7} {'edge':>7} {'ROI':>8} {'±2se':>6} "
          f"{'move':>6} {'medΔm':>6}")
    for key in order:
        a = cells.get(key)
        if not a or a.n < min_n:
            continue
        n = a.n
        win = a.wins / n
        avg = a.sum_p / n
        roi = a.sum_ret / n
        delays = sorted(a.delays)
        med = delays[len(delays) // 2] / 60
        print(f"{key:28} {n:6d} {100 * win:6.1f}% {avg:7.3f} {win - avg:+7.3f} {100 * roi:+7.1f}% "
              f"{se2(win, n):6.3f} {a.sum_move / n:6.3f} {med:5.0f}m")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-volume", type=float, default=5000)
    ap.add_argument("--min-n", type=int, default=40)
    args = ap.parse_args()

    conn = storage.connect(storage.db_path("tennis"))
    conn.execute("CREATE INDEX IF NOT EXISTS pm_prices_token_t ON pm_prices(token_id, t)")
    conn.commit()

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
    print(f"candidates: {len(rows):,}", flush=True)

    # strategy -> group -> Agg
    strat: dict[str, dict[str, Agg]] = defaultdict(lambda: defaultdict(Agg))
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

        series_f = series_a if fav_is_a else [(t, 1 - p) for t, p in series_a]
        det = detect_set1_end(series_f, start, wins[0])
        if not det:
            skipped["detect"] += 1
            continue
        entry_t, p_fav, move = det
        delay = entry_t - start
        dog_p = 1 - p_fav
        if not 0.02 < dog_p < 0.98:
            skipped["detect"] += 1
            continue

        fb = band_fav(fav_p0) or "?"
        db = band_dog(1 / (1 - fav_p0)) or "?"
        surf = surface or "?"
        tour = series_of(raw)
        tier = tier_of(level, tour)
        tags = ["all", f"fav {fb}", f"dog {db}", f"surf {surf}", f"tier {tier}", f"tour {tour}",
                f"{fb} × {surf}", f"{fb} × {tier}", f"{db} × {surf}", f"{db} × {tier}",
                f"{db} × {tour}"]

        fav_won_s1 = wins[0]
        if fav_won_s1:
            # fav_continue: buy fav at p_fav
            for g in tags:
                strat["fav_continue"][g].add(p_fav, fav_won, move, delay)
                strat["dog_upset"][g].add(dog_p, not fav_won, move, delay)
        else:
            for g in tags:
                strat["fav_comeback"][g].add(p_fav, fav_won, move, delay)
                strat["dog_close"][g].add(dog_p, not fav_won, move, delay)

    print(f"skipped: {dict(skipped)}", flush=True)
    print(f"\nSet-1 timing: max move ≥{MIN_MOVE} in [{SET1_LO // 60},{SET1_HI // 60}] min "
          f"after start, entry +{SETTLE}s. Fee {FEE}.\n")

    orders = {
        "all": ["all"],
        "fav odds": [f"fav {b[2]}" for b in FAV_BANDS],
        "dog odds": [f"dog {b[2]}" for b in DOG_BANDS],
        "surface": [f"surf {s}" for s in SURF_ORDER],
        "tier": [f"tier {t}" for t in TIER_ORDER],
        "tour (ATP/WTA/ITF)": [f"tour {s}" for s in SERIES_ORDER],
        "fav × surface": [f"{b[2]} × {s}" for b in FAV_BANDS for s in SURF_ORDER],
        "fav × tier": [f"{b[2]} × {t}" for b in FAV_BANDS for t in TIER_ORDER],
        "dog × surface": [f"{b[2]} × {s}" for b in DOG_BANDS for s in SURF_ORDER],
        "dog × tier": [f"{b[2]} × {t}" for b in DOG_BANDS for t in TIER_ORDER],
        "dog × tour": [f"{b[2]} × {s}" for b in DOG_BANDS for s in SERIES_ORDER],
    }

    for name, label in (
        ("dog_close", "BUY UNDERDOG after underdog took set 1 (dog closes)"),
        ("fav_comeback", "BUY FAVORITE after favorite lost set 1 (comeback)"),
        ("fav_continue", "BUY FAVORITE after favorite won set 1"),
        ("dog_upset", "BUY UNDERDOG after favorite won set 1 (upset)"),
    ):
        print(f"\n########## {label} ##########")
        cells = strat[name]
        for cut, order in orders.items():
            # only show rich cuts for the two main live strategies
            if cut in ("fav × surface", "fav × tier", "dog × surface", "dog × tier") and name not in (
                "dog_close", "fav_comeback",
            ):
                continue
            print_roi(f"{name} / {cut}", cells, order, args.min_n)

    # fair-vs-market for dog_close: compare win% to R-22 frequencies
    print("\n=== dog_close: market price vs historical close rate (R-22) ===")
    print(f"{'group':28} {'n':>6} {'mkt p':>7} {'win%':>7} {'R22 close':>10} {'vs R22':>8} {'ROI':>8}")
    # R-22 dog close rates by dog odds band (from previous analysis)
    r22 = {
        "dog 2.0–2.5": 0.749,
        "dog 2.5–3.33": 0.676,
        "dog 3.33–5.0": 0.580,
        "dog 5–10": 0.481,
        "dog 10+": 0.390,
        "all": 0.659,
        "surf Hard": 0.660,
        "surf Clay": 0.656,
        "tier slam": 0.603,
        "tier masters": 0.670,
        "tier challenger": 0.676,
        "tier itf": 0.633,
    }
    for key in ["all"] + [f"dog {b[2]}" for b in DOG_BANDS] + [f"surf {s}" for s in SURF_ORDER] + [
        f"tier {t}" for t in TIER_ORDER
    ]:
        a = strat["dog_close"].get(key)
        if not a or a.n < args.min_n:
            continue
        win = a.wins / a.n
        avg = a.sum_p / a.n
        roi = a.sum_ret / a.n
        fair = r22.get(key)
        vs = f"{win - fair:+.3f}" if fair is not None else "—"
        fair_s = f"{100 * fair:5.1f}%" if fair is not None else "   —"
        print(f"{key:28} {a.n:6d} {avg:7.3f} {100 * win:6.1f}% {fair_s:>10} {vs:>8} {100 * roi:+7.1f}%")
    print()


if __name__ == "__main__":
    main()
