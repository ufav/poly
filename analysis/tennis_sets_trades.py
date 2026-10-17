"""R-23 on real trades: ROI after set 1 using VWAP in a window after the set-1 price jump.

Timing of set 1 = same mid-price detector as tennis_sets_live.py. Executable price = notional-weighted
VWAP of taker trades in [entry, entry+3min] (fallback [entry-1min, entry+5min]), converted to P(fav).

  python analysis/tennis_sets_trades.py [--min-volume 50000]
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
from analysis.tennis_sets_live import MIN_MOVE, SET1_HI, SET1_LO, SETTLE, detect_set1_end, fee  # noqa: E402

WIN_PRIMARY = 180
WIN_FALLBACK = (-60, 300)


def vwap_p_a(conn, cond: str, tok_a: str, t0: int, t1: int) -> tuple[float | None, int]:
    """Notional VWAP implied P(team_a); returns (p, n_trades)."""
    rows = conn.execute(
        """SELECT asset, side, price, size FROM pm_trades
           WHERE condition_id = ? AND t BETWEEN ? AND ?""",
        (cond, t0, t1),
    ).fetchall()
    if not rows:
        return None, 0
    num = den = 0.0
    for asset, side, price, size in rows:
        on_a = (asset == tok_a) == (side == "BUY")
        p = price if on_a else 1 - price
        w = price * size
        if w <= 0:
            continue
        num += p * w
        den += w
    if den <= 0:
        return None, 0
    return num / den, len(rows)


def entry_p_fav(conn, cond: str, tok_a: str, fav_is_a: bool, entry_t: int
                ) -> tuple[float | None, int, str]:
    p_a, n = vwap_p_a(conn, cond, tok_a, entry_t, entry_t + WIN_PRIMARY)
    which = "3m"
    if p_a is None:
        p_a, n = vwap_p_a(conn, cond, tok_a, entry_t + WIN_FALLBACK[0], entry_t + WIN_FALLBACK[1])
        which = "fb"
    if p_a is None or not 0.02 < p_a < 0.98:
        return None, 0, which
    p_fav = p_a if fav_is_a else 1 - p_a
    if not 0.02 < p_fav < 0.98:
        return None, 0, which
    return p_fav, n, which


class Agg:
    __slots__ = ("n", "wins", "sum_p", "sum_ret", "sum_move", "delays", "n_tr")

    def __init__(self) -> None:
        self.n = self.wins = self.n_tr = 0
        self.sum_p = self.sum_ret = self.sum_move = 0.0
        self.delays: list[int] = []

    def add(self, p: float, won: bool, move: float, delay: int, n_tr: int) -> None:
        self.n += 1
        self.wins += int(won)
        self.sum_p += p
        self.sum_ret += (1 if won else 0) / (p + fee(p)) - 1
        self.sum_move += move
        self.delays.append(delay)
        self.n_tr += n_tr


def print_roi(title: str, cells: dict[str, Agg], order: list[str], min_n: int = 30) -> None:
    print(f"\n=== {title} ===")
    print(f"{'group':28} {'n':>5} {'win%':>7} {'avg p':>7} {'edge':>7} {'ROI':>8} {'±2se':>6} "
          f"{'move':>5} {'medΔ':>5} {'tr/m':>5}")
    for key in order:
        a = cells.get(key)
        if not a or a.n < min_n:
            continue
        n = a.n
        win = a.wins / n
        avg = a.sum_p / n
        roi = a.sum_ret / n
        med = sorted(a.delays)[len(a.delays) // 2] / 60
        print(f"{key:28} {n:5d} {100 * win:6.1f}% {avg:7.3f} {win - avg:+7.3f} {100 * roi:+7.1f}% "
              f"{se2(win, n):6.3f} {a.sum_move / n:5.3f} {med:4.0f}m {a.n_tr / n:5.1f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-volume", type=float, default=50_000)
    ap.add_argument("--min-n", type=int, default=30)
    args = ap.parse_args()

    conn = storage.connect(storage.db_path("tennis"))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = conn.execute(
        """SELECT e.start_time, e.raw, e.score, m.condition_id, m.token_a, m.final_price_a,
                  l.surface, l.level, l.side_flip, ob.set_scores
           FROM pm_markets m
           JOIN pm_events e ON e.id = m.event_id
           JOIN tennis_links l ON l.pm_event_id = e.id
           LEFT JOIN ob_events ob ON ob.id = l.ob_event_id
           WHERE m.market_type = 'moneyline' AND m.trades_status = 'done'
             AND m.final_price_a IN (0, 1) AND m.volume >= ?
             AND e.start_time < ? AND l.ob_event_id IS NOT NULL""",
        (args.min_volume, now),
    ).fetchall()
    print(f"markets with trades: {len(rows):,} (vol >= ${args.min_volume:,.0f})", flush=True)
    print(f"set1 detect: max mid move ≥{MIN_MOVE} in [{SET1_LO // 60},{SET1_HI // 60}]m +{SETTLE}s; "
          f"VWAP trades [{WIN_PRIMARY}s] / fallback {WIN_FALLBACK}\n", flush=True)

    strat: dict[str, dict[str, Agg]] = defaultdict(lambda: defaultdict(Agg))
    skipped = defaultdict(int)
    win_src = defaultdict(int)

    for i, (start_time, raw, score, cond, token, final_a, surface, level, side_flip,
            ob_scores) in enumerate(rows):
        if i and i % 1000 == 0:
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
        entry_t, _mid_p, move = det

        p_fav, n_tr, src = entry_p_fav(conn, cond, token, fav_is_a, entry_t)
        if p_fav is None:
            skipped["no_trades"] += 1
            continue
        win_src[src] += 1
        dog_p = 1 - p_fav
        delay = entry_t - start

        fb = band_fav(fav_p0) or "?"
        db = band_dog(1 / (1 - fav_p0)) or "?"
        surf = surface or "?"
        tour = series_of(raw)
        tier = tier_of(level, tour)
        tags = ["all", f"fav {fb}", f"dog {db}", f"surf {surf}", f"tier {tier}", f"tour {tour}",
                f"{db} × {surf}", f"{db} × {tier}", f"{db} × {tour}", f"tour {tour} × {surf}"]

        if wins[0]:
            for g in tags:
                strat["fav_continue"][g].add(p_fav, fav_won, move, delay, n_tr)
                strat["dog_upset"][g].add(dog_p, not fav_won, move, delay, n_tr)
        else:
            for g in tags:
                strat["fav_comeback"][g].add(p_fav, fav_won, move, delay, n_tr)
                strat["dog_close"][g].add(dog_p, not fav_won, move, delay, n_tr)

    print(f"skipped: {dict(skipped)}  vwap windows: {dict(win_src)}", flush=True)

    orders = {
        "all": ["all"],
        "fav odds": [f"fav {b[2]}" for b in FAV_BANDS],
        "dog odds": [f"dog {b[2]}" for b in DOG_BANDS],
        "surface": [f"surf {s}" for s in SURF_ORDER],
        "tier": [f"tier {t}" for t in TIER_ORDER],
        "tour (ATP/WTA/ITF)": [f"tour {s}" for s in SERIES_ORDER],
        "dog × surface": [f"{b[2]} × {s}" for b in DOG_BANDS for s in SURF_ORDER],
        "dog × tier": [f"{b[2]} × {t}" for b in DOG_BANDS for t in TIER_ORDER],
        "dog × tour": [f"{b[2]} × {s}" for b in DOG_BANDS for s in SERIES_ORDER],
        "tour × surface": [f"tour {s} × {u}" for s in SERIES_ORDER for u in SURF_ORDER],
    }

    for name, label in (
        ("dog_close", "BUY UNDERDOG after underdog took set 1 — TRADE VWAP"),
        ("fav_comeback", "BUY FAVORITE after favorite lost set 1 — TRADE VWAP"),
        ("fav_continue", "BUY FAVORITE after favorite won set 1 — TRADE VWAP"),
        ("dog_upset", "BUY UNDERDOG after favorite won set 1 — TRADE VWAP"),
    ):
        print(f"\n########## {label} ##########")
        cells = strat[name]
        for cut, order in orders.items():
            if cut.startswith("dog ×") and name not in ("dog_close", "fav_comeback"):
                continue
            if cut == "tour × surface" and name not in ("dog_close", "fav_continue"):
                continue
            print_roi(f"{name} / {cut}", cells, order, args.min_n)

    # side-by-side with mid R-23 headline
    print("\n=== dog_close vs mid R-23 (same logic, executable VWAP) ===")
    print(f"{'group':28} {'n':>5} {'mkt p':>7} {'win%':>7} {'ROI':>8}")
    for key in (["all"] + [f"dog {b[2]}" for b in DOG_BANDS] + [f"surf {s}" for s in SURF_ORDER]
                + [f"tier {t}" for t in ("slam", "masters", "challenger", "itf")]
                + [f"tour {s}" for s in SERIES_ORDER]):
        a = strat["dog_close"].get(key)
        if not a or a.n < args.min_n:
            continue
        print(f"{key:28} {a.n:5d} {a.sum_p / a.n:7.3f} {100 * a.wins / a.n:6.1f}% "
              f"{100 * a.sum_ret / a.n:+7.1f}%")
    print()

    print("\n=== ATP vs WTA headline (trade VWAP) ===")
    print(f"{'strategy':40} {'tour':6} {'n':>5} {'avg p':>7} {'win%':>7} {'ROI':>8}")
    for name in ("dog_close", "fav_continue", "fav_comeback", "dog_upset"):
        for tour in SERIES_ORDER:
            a = strat[name].get(f"tour {tour}")
            if not a or a.n < args.min_n:
                continue
            print(f"{name:40} {tour:6} {a.n:5d} {a.sum_p / a.n:7.3f} {100 * a.wins / a.n:6.1f}% "
                  f"{100 * a.sum_ret / a.n:+7.1f}%")
    print()


if __name__ == "__main__":
    main()
