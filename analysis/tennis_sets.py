"""Tennis set paths by pre-match odds band, surface and tournament tier (A-46).

Favorite / underdog from moneyline mid-price at scheduled start. Set scores from Polymarket
`score` or Olimpbet `set_scores` (with side_flip). Skips incomplete / retired sets.

  python analysis/tennis_sets.py [--min-volume 5000]
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

# favorite price bands (implied win prob at start)
FAV_BANDS = [
    (0.50, 0.55, "1.82–2.00"),   # slight fav / coin-flip
    (0.55, 0.60, "1.67–1.82"),
    (0.60, 0.70, "1.43–1.67"),
    (0.70, 0.80, "1.25–1.43"),
    (0.80, 0.90, "1.11–1.25"),
    (0.90, 0.98, "1.02–1.11"),
]

# underdog decimal odds bands
DOG_BANDS = [
    (2.0, 2.5, "2.0–2.5"),
    (2.5, 3.33, "2.5–3.33"),
    (3.33, 5.0, "3.33–5.0"),
    (5.0, 10.0, "5–10"),
    (10.0, 100.0, "10+"),
]

TIER_ORDER = ("slam", "masters", "500", "250/125", "challenger", "itf")
SERIES_ORDER = ("atp", "wta", "itf")
SURF_ORDER = ("Hard", "Clay", "Grass")


def series_of(raw: str | None) -> str:
    try:
        return (json.loads(raw) if isinstance(raw, str) else raw or {}).get("seriesSlug") or "?"
    except (TypeError, json.JSONDecodeError):
        return "?"


def tier_of(level: str | None, series: str | None = None) -> str:
    s = (level or "").lower()
    if any(k in s for k in ("australian open", "roland garros", "wimbledon", "us open", "french open")):
        return "slam"
    if "finals" in s or "laver cup" in s:
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
    if series == "itf":
        return "itf"
    return "other"


def se2(p: float, n: int) -> float:
    return 2 * (p * (1 - p) / n) ** 0.5 if n else 0.0


def set_complete(a: int, b: int) -> bool:
    hi, lo = max(a, b), min(a, b)
    if hi < 6:
        return False
    if hi == 6:
        return hi - lo >= 2
    if hi == 7:
        return lo in (5, 6)
    return hi - lo >= 2  # rare e.g. 8-6 old scoring


def parse_sets_pm(score: str | None) -> list[tuple[int, int]] | None:
    if not score:
        return None
    out = []
    for part in score.split(","):
        m = re.match(r"\s*(\d+)\s*-\s*(\d+)", part)
        if not m:
            return None
        out.append((int(m.group(1)), int(m.group(2))))
    return out or None


def parse_sets_ob(raw: str | None, side_flip: int | None) -> list[tuple[int, int]] | None:
    if not raw:
        return None
    try:
        sets = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    out = []
    for pair in sets:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            return None
        a, b = int(pair[0]), int(pair[1])
        if side_flip:
            a, b = b, a
        out.append((a, b))
    return out or None


def fav_set_wins(sets_ab: list[tuple[int, int]], fav_is_a: bool) -> list[bool] | None:
    """Per-set True if favorite won the set; None if any set incomplete."""
    wins = []
    for a, b in sets_ab:
        if not set_complete(a, b):
            return None
        a_won = a > b
        wins.append(a_won if fav_is_a else (not a_won))
    return wins if len(wins) >= 2 else None


def path_label(wins: list[bool]) -> str:
    """W/L string from favorite's view, truncated to relevant BO3/BO5."""
    return "".join("W" if x else "L" for x in wins)


def band_fav(p: float) -> str | None:
    for lo, hi, label in FAV_BANDS:
        if lo <= p < hi:
            return label
    return None


def band_dog(odds: float) -> str | None:
    for lo, hi, label in DOG_BANDS:
        if lo <= odds < hi:
            return label
    return None


class Cell:
    __slots__ = ("n", "s1", "match", "s1_then_match", "lose_s1", "lose_s1_win_s2",
                 "lose_s1_win_match", "win_s1_win_match", "paths")

    def __init__(self) -> None:
        self.n = 0
        self.s1 = 0                 # fav won set 1
        self.match = 0              # fav won match
        self.s1_then_match = 0      # unused placeholder
        self.lose_s1 = 0
        self.lose_s1_win_s2 = 0
        self.lose_s1_win_match = 0
        self.win_s1_win_match = 0
        self.paths: dict[str, int] = defaultdict(int)

    def add(self, wins: list[bool], fav_won_match: bool) -> None:
        self.n += 1
        if wins[0]:
            self.s1 += 1
            if fav_won_match:
                self.win_s1_win_match += 1
        else:
            self.lose_s1 += 1
            if len(wins) >= 2 and wins[1]:
                self.lose_s1_win_s2 += 1
            if fav_won_match:
                self.lose_s1_win_match += 1
        if fav_won_match:
            self.match += 1
        # normalize path: for BO3 stop at decision
        label = path_label(wins)
        if label.startswith("WW"):
            self.paths["WW"] += 1
        elif label.startswith("LL"):
            self.paths["LL"] += 1
        elif label.startswith("WLW"):
            self.paths["WLW"] += 1
        elif label.startswith("WLL"):
            self.paths["WLL"] += 1
        elif label.startswith("LWW"):
            self.paths["LWW"] += 1
        elif label.startswith("LWL"):
            self.paths["LWL"] += 1
        elif label.startswith("WL"):
            self.paths["WL?"] += 1
        elif label.startswith("LW"):
            self.paths["LW?"] += 1
        else:
            self.paths[label[:5]] += 1


def pct(k: int, n: int) -> str:
    return f"{100 * k / n:5.1f}%" if n else "   —"


def print_table(title: str, cells: dict[str, Cell], order: list[str], min_n: int = 40) -> None:
    print(f"\n=== {title} ===")
    print(f"{'group':16} {'n':>6} {'fav S1':>8} {'fav match':>10} "
          f"{'P(M|S1)':>8} {'P(M|~S1)':>9} {'P(S2|~S1)':>10} "
          f"{'WW':>5} {'LL':>5} {'WLW':>5} {'LWW':>5} {'WLL':>5} {'LWL':>5}")
    for key in order:
        c = cells.get(key)
        if not c or c.n < min_n:
            continue
        n = c.n
        p_m_s1 = c.win_s1_win_match / c.s1 if c.s1 else 0
        p_m_ls1 = c.lose_s1_win_match / c.lose_s1 if c.lose_s1 else 0
        p_s2 = c.lose_s1_win_s2 / c.lose_s1 if c.lose_s1 else 0
        print(f"{key:16} {n:6d} {pct(c.s1, n):>8} {pct(c.match, n):>10} "
              f"{pct(c.win_s1_win_match, c.s1) if c.s1 >= max(20, min_n // 2) else '   —':>8} "
              f"{pct(c.lose_s1_win_match, c.lose_s1) if c.lose_s1 >= max(20, min_n // 2) else '   —':>9} "
              f"{pct(c.lose_s1_win_s2, c.lose_s1) if c.lose_s1 >= max(20, min_n // 2) else '   —':>10} "
              f"{c.paths.get('WW', 0):5d} {c.paths.get('LL', 0):5d} "
              f"{c.paths.get('WLW', 0):5d} {c.paths.get('LWW', 0):5d} "
              f"{c.paths.get('WLL', 0):5d} {c.paths.get('LWL', 0):5d}")


def print_comeback(title: str, cells: dict[str, Cell], order: list[str], min_n: int = 40) -> None:
    """Focus: favorite lost set 1 — how often wins set 2 and the match (and underdog mirror)."""
    print(f"\n=== {title} ===")
    print(f"{'group':16} {'n':>6} {'lost S1':>8} {'then S2':>8} {'then match':>11} "
          f"{'dog won S1':>11} {'dog closed':>11}")
    for key in order:
        c = cells.get(key)
        if not c or c.n < min_n:
            continue
        # dog won S1 = fav lost S1; dog closed = fav lost S1 and fav lost match
        dog_closed = c.lose_s1 - c.lose_s1_win_match
        print(f"{key:16} {c.n:6d} {pct(c.lose_s1, c.n):>8} "
              f"{pct(c.lose_s1_win_s2, c.lose_s1) if c.lose_s1 else '   —':>8} "
              f"{pct(c.lose_s1_win_match, c.lose_s1) if c.lose_s1 else '   —':>11} "
              f"{pct(c.lose_s1, c.n):>11} "
              f"{pct(dog_closed, c.lose_s1) if c.lose_s1 else '   —':>11}")


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
        """SELECT e.id, e.start_time, e.raw, e.score, e.volume,
                  m.token_a, m.final_price_a,
                  l.surface, l.level, l.side_flip,
                  ob.set_scores
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
    print(f"candidate markets: {len(rows):,} (vol >= ${args.min_volume:,.0f})", flush=True)

    by_fav: dict[str, Cell] = defaultdict(Cell)
    by_dog: dict[str, Cell] = defaultdict(Cell)
    by_surf: dict[str, Cell] = defaultdict(Cell)
    by_tier: dict[str, Cell] = defaultdict(Cell)
    by_fav_surf: dict[str, Cell] = defaultdict(Cell)
    by_fav_tier: dict[str, Cell] = defaultdict(Cell)
    by_dog_surf: dict[str, Cell] = defaultdict(Cell)
    by_dog_tier: dict[str, Cell] = defaultdict(Cell)
    by_fav_surf_tier: dict[str, Cell] = defaultdict(Cell)
    all_c = Cell()

    used = skipped_price = skipped_sets = 0
    for i, (_eid, start_time, raw, score, _vol, token, final_a, surface, level,
            side_flip, ob_scores) in enumerate(rows):
        if i and i % 2000 == 0:
            print(f"  … {i}/{len(rows)}", flush=True)
        start = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
        pre = conn.execute(
            "SELECT p FROM pm_prices WHERE token_id = ? AND t <= ? ORDER BY t DESC LIMIT 1",
            (token, start),
        ).fetchone()
        if not pre or not 0.02 < pre[0] < 0.98:
            skipped_price += 1
            continue
        p_a = pre[0]
        fav_is_a = p_a >= 0.5
        fav_p = p_a if fav_is_a else 1 - p_a
        dog_p = 1 - fav_p
        fav_won = (final_a == 1) if fav_is_a else (final_a == 0)

        sets_ab = parse_sets_pm(score) or parse_sets_ob(ob_scores, side_flip)
        if not sets_ab:
            skipped_sets += 1
            continue
        wins = fav_set_wins(sets_ab, fav_is_a)
        if not wins:
            skipped_sets += 1
            continue

        # consistency: match winner from sets vs market (allow BO5)
        sets_fav = sum(wins)
        sets_dog = len(wins) - sets_fav
        if sets_fav == sets_dog:
            skipped_sets += 1
            continue
        sets_say_fav = sets_fav > sets_dog
        if sets_say_fav != fav_won:
            # retirement / walkover mismatch — drop
            skipped_sets += 1
            continue

        used += 1
        fb = band_fav(fav_p)
        db = band_dog(1 / dog_p)
        surf = surface or "?"
        series = series_of(raw)
        tier = tier_of(level, series)

        all_c.add(wins, fav_won)
        if fb:
            by_fav[fb].add(wins, fav_won)
            by_fav_surf[f"{fb} | {surf}"].add(wins, fav_won)
            by_fav_tier[f"{fb} | {tier}"].add(wins, fav_won)
            by_fav_surf_tier[f"{fb} | {surf} | {tier}"].add(wins, fav_won)
        if db:
            by_dog[db].add(wins, fav_won)
            by_dog_surf[f"{db} | {surf}"].add(wins, fav_won)
            by_dog_tier[f"{db} | {tier}"].add(wins, fav_won)
        by_surf[surf].add(wins, fav_won)
        by_tier[tier].add(wins, fav_won)

    print(f"used: {used:,}  no price: {skipped_price:,}  bad/incomplete sets: {skipped_sets:,}", flush=True)

    print("\nLegend (always from FAVORITE's view at pre-match mid price):")
    print("  fav S1     = favorite won set 1")
    print("  fav match  = favorite won the match")
    print("  P(M|S1)    = win match | won set 1")
    print("  P(M|~S1)   = win match | lost set 1  (comeback rate)")
    print("  P(S2|~S1)  = win set 2 | lost set 1")
    print("  WW/LL/WLW/LWW/WLL/LWL = set path counts")

    print_table("ALL", {"all": all_c}, ["all"], args.min_n)

    print_table(
        "By FAVORITE odds (decimal = 1/price)",
        by_fav,
        [b[2] for b in FAV_BANDS],
        args.min_n,
    )
    print_table(
        "By UNDERDOG odds (favorite is the other side)",
        by_dog,
        [b[2] for b in DOG_BANDS],
        args.min_n,
    )
    print_table("By surface", by_surf, list(SURF_ORDER) + ["Carpet", "?"], args.min_n)
    print_table("By tournament tier", by_tier, list(TIER_ORDER) + ["finals", "other"], args.min_n)

    # favorite band × surface
    order_fs = [f"{b[2]} | {s}" for b in FAV_BANDS for s in SURF_ORDER]
    print_table("Favorite odds × surface", by_fav_surf, order_fs, args.min_n)

    order_ft = [f"{b[2]} | {t}" for b in FAV_BANDS for t in TIER_ORDER]
    print_table("Favorite odds × tier", by_fav_tier, order_ft, args.min_n)

    order_ds = [f"{b[2]} | {s}" for b in DOG_BANDS for s in SURF_ORDER]
    print_table("Underdog odds × surface", by_dog_surf, order_ds, args.min_n)

    order_dt = [f"{b[2]} | {t}" for b in DOG_BANDS for t in TIER_ORDER]
    print_table("Underdog odds × tier", by_dog_tier, order_dt, args.min_n)

    # highlight cells: fav band × surf × tier for main bands
    order_fst = []
    for band in ("1.43–1.67", "1.25–1.43", "1.11–1.25"):
        for s in SURF_ORDER:
            for t in ("slam", "masters", "challenger", "itf"):
                order_fst.append(f"{band} | {s} | {t}")
    print_table("Favorite odds × surface × tier (selected)", by_fav_surf_tier, order_fst, max(30, args.min_n // 2))

    print_comeback(
        "Comeback lens: favorite lost set 1",
        {**{"all": all_c}, **by_fav, **by_surf, **by_tier},
        ["all"] + [b[2] for b in FAV_BANDS] + list(SURF_ORDER) + list(TIER_ORDER),
        args.min_n,
    )

    # Underdog who won set 1 — close rate (same as fav lost S1 and lost match)
    print("\n=== Underdog takes set 1 — how often closes the match ===")
    print(f"{'group':16} {'dog S1 n':>8} {'dog wins match':>15} {'±2se':>6}")
    for key in ["all"] + [b[2] for b in DOG_BANDS] + list(SURF_ORDER) + list(TIER_ORDER):
        c = {"all": all_c, **by_dog, **by_surf, **by_tier}.get(key)
        if not c or c.lose_s1 < args.min_n:
            continue
        # when fav lost S1, dog won S1; dog wins match = fav did not win match after lose S1
        dog_win = c.lose_s1 - c.lose_s1_win_match
        rate = dog_win / c.lose_s1
        print(f"{key:16} {c.lose_s1:8d} {pct(dog_win, c.lose_s1):>15} {se2(rate, c.lose_s1):6.3f}")

    print()


if __name__ == "__main__":
    main()
