"""Paper tennis live_copy: ROI by prematch / entry odds for filled trades.

  python analysis/paper_tennis_band.py [--db data/live_tennis_copy.sqlite]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def odds(p: float | None) -> float | None:
    if not p or p <= 0:
        return None
    return 1.0 / p


def load(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT strategy, outcome, state, avg_price, best_ask, stake, fee, pnl, status, filled, signal_t
           FROM paper_trades WHERE filled > 0 AND avg_price IS NOT NULL"""
    ).fetchall()
    out = []
    for strategy, outcome, state, avg, ask, stake, fee, pnl, status, filled, signal_t in rows:
        st = json.loads(state or "{}")
        pm = st.get("prematch") or {}
        rounds = st.get("rounds") or {}
        if outcome not in pm:
            continue
        bought_pm = float(pm[outcome])
        other = next((t for t in pm if t != outcome), None)
        games_w = int(rounds.get(outcome, 0)) if rounds else None
        games_l = int(rounds.get(other, 0)) if other and rounds else None
        out.append({
            "strategy": strategy,
            "bought_pm": bought_pm,
            "bought_odds": odds(bought_pm),
            "avg_price": float(avg),
            "entry_odds": odds(float(avg)),
            "ask": float(ask) if ask is not None else float(avg),
            "stake": float(stake or 0),
            "pnl": float(pnl) if pnl is not None else None,
            "settled": status == "settled" and pnl is not None,
            "won": (pnl or 0) > 0 if status == "settled" and pnl is not None else None,
            "games_score": f"{games_w}-{games_l}" if games_w is not None and games_l is not None else None,
            "games_w": games_w,
            "signal_t": signal_t,
        })
    return out


def bucket_odds(o: float) -> str:
    """Prematch decimal-odds buckets (human)."""
    bands = [
        (1.01, 1.15, "1.01–1.15"),
        (1.15, 1.25, "1.15–1.25"),
        (1.25, 1.40, "1.25–1.40"),
        (1.40, 1.60, "1.40–1.60"),
        (1.60, 1.80, "1.60–1.80"),
        (1.80, 2.00, "1.80–2.00"),
        (2.00, 2.50, "2.00–2.50"),
        (2.50, 3.50, "2.50–3.50"),
        (3.50, 99.0, "3.50+"),
    ]
    for lo, hi, name in bands:
        if lo <= o < hi:
            return name
    return "?"


def entry_bucket(o: float) -> str:
    bands = [
        (1.01, 1.10, "1.01–1.10"),
        (1.10, 1.20, "1.10–1.20"),
        (1.20, 1.35, "1.20–1.35"),
        (1.35, 1.55, "1.35–1.55"),
        (1.55, 1.80, "1.55–1.80"),
        (1.80, 2.20, "1.80–2.20"),
        (2.20, 99.0, "2.20+"),
    ]
    for lo, hi, name in bands:
        if lo <= o < hi:
            return name
    return "?"


def summarize(trades: list[dict], key_fn, order: list[str] | None = None):
    g: dict[str, list] = defaultdict(list)
    for t in trades:
        if not t["settled"]:
            continue
        k = key_fn(t)
        if k:
            g[k].append(t)
    keys = order or sorted(g.keys())
    rows = []
    for k in keys:
        xs = g.get(k) or []
        if not xs:
            continue
        n = len(xs)
        w = sum(1 for t in xs if t["won"])
        stake = sum(t["stake"] for t in xs)
        pnl = sum(t["pnl"] for t in xs)
        rows.append((k, n, w, n - w, stake, pnl, pnl / stake if stake else 0, w / n if n else 0))
    return rows


def print_rows(title: str, rows, min_n: int = 3):
    print(f"\n{title}")
    print(f"  {'bucket':<14} {'n':>4} {'W-L':>7} {'stake':>8} {'pnl':>9} {'ROI':>7} {'WR':>6}")
    for k, n, w, l, stake, pnl, roi, wr in rows:
        if n < min_n:
            continue
        print(f"  {k:<14} {n:4d} {w:3d}-{l:<3d} {stake:8.0f} {pnl:9.1f} {roi*100:6.1f}% {wr*100:5.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/live_tennis_copy.sqlite")
    args = ap.parse_args()
    path = Path(args.db)
    if not path.exists():
        sys.exit(f"no db: {path}")

    conn = sqlite3.connect(path)
    trades = load(conn)
    settled = [t for t in trades if t["settled"]]
    print(f"db={path}  filled={len(trades)}  settled={len(settled)}")
    if settled:
        a = min(t["signal_t"] for t in settled)
        b = max(t["signal_t"] for t in settled)
        print(f"signals UTC: {datetime.fromtimestamp(a, timezone.utc):%Y-%m-%d} .. "
              f"{datetime.fromtimestamp(b, timezone.utc):%Y-%m-%d}")

    by = defaultdict(list)
    for t in trades:
        by[t["strategy"]].append(t)

    print("\n=== стратегии ===")
    print(f"  {'strategy':<20} {'n':>4} {'W-L':>7} {'pnl':>9} {'ROI':>7} {'avg_entry':>10} {'avg_pm':>8}")
    for s in sorted(by):
        g = [t for t in by[s] if t["settled"]]
        if not g:
            continue
        stake = sum(t["stake"] for t in g)
        pnl = sum(t["pnl"] for t in g)
        w = sum(1 for t in g if t["won"])
        ae = sum(t["entry_odds"] for t in g) / len(g)
        apm = sum(t["bought_odds"] for t in g) / len(g)
        print(f"  {s:<20} {len(g):4d} {w:3d}-{len(g)-w:<3d} {pnl:9.1f} {pnl/stake*100:6.1f}% "
              f"{ae:10.2f} {apm:8.2f}")

    # note: s1_winner overlaps fav+dog
    fav = [t for t in by["tennis_fav_s1"] if t["settled"]]
    dog = [t for t in by["tennis_dog_s1"] if t["settled"]]
    any_ = [t for t in by["tennis_s1_winner"] if t["settled"]]
    print(f"\nперекрытие: fav_s1={len(fav)} dog_s1={len(dog)} s1_winner={len(any_)} "
          f"(s1_winner ≈ fav+dog по смыслу)")

    pm_order = ["1.01–1.15", "1.15–1.25", "1.25–1.40", "1.40–1.60", "1.60–1.80",
                "1.80–2.00", "2.00–2.50", "2.50–3.50", "3.50+"]
    en_order = ["1.01–1.10", "1.10–1.20", "1.20–1.35", "1.35–1.55", "1.55–1.80", "1.80–2.20", "2.20+"]

    for name in ("tennis_fav_s1", "tennis_s1_winner", "tennis_dog_s1"):
        g = by.get(name) or []
        print(f"\n{'='*60}\n{name}")
        print_rows("прематч купленного (кэф)", summarize(g, lambda t: bucket_odds(t["bought_odds"]), pm_order))
        print_rows("цена входа (кэф)", summarize(g, lambda t: entry_bucket(t["entry_odds"]), en_order))
        print_rows("счёт сета 1 (геймы)", summarize(g, lambda t: t["games_score"]))

    # combined unique matches: prefer looking at fav_s1 + dog only (no double count)
    uniq = fav + dog
    print(f"\n{'='*60}\nfav+dog без двойного счёта (n={len(uniq)})")
    print_rows("прематч", summarize(uniq, lambda t: bucket_odds(t["bought_odds"]), pm_order))
    print_rows("вход", summarize(uniq, lambda t: entry_bucket(t["entry_odds"]), en_order), min_n=2)

    # best contiguous prematch odds for fav
    print("\n--- лучшие окна прематч fav_s1 (кэф) ---")
    edges = [1.05, 1.10, 1.15, 1.20, 1.25, 1.30, 1.40, 1.50, 1.60, 1.80, 2.00]
    best = None
    for i, lo in enumerate(edges):
        for hi in edges[i + 1:]:
            g = [t for t in fav if lo <= t["bought_odds"] < hi]
            if len(g) < 10:
                continue
            stake = sum(t["stake"] for t in g)
            pnl = sum(t["pnl"] for t in g)
            roi = pnl / stake
            rec = (lo, hi, len(g), pnl, roi)
            if best is None or roi > best[4]:
                best = rec
    if best:
        lo, hi, n, pnl, roi = best
        print(f"  лучший: кэф {lo:.2f}–{hi:.2f}  n={n}  pnl={pnl:.1f}  ROI={roi*100:.1f}%")

    best_e = None
    for i, lo in enumerate(edges):
        for hi in edges[i + 1:]:
            g = [t for t in fav if lo <= t["entry_odds"] < hi]
            if len(g) < 10:
                continue
            stake = sum(t["stake"] for t in g)
            pnl = sum(t["pnl"] for t in g)
            roi = pnl / stake
            rec = (lo, hi, len(g), pnl, roi)
            if best_e is None or roi > best_e[4]:
                best_e = rec
    if best_e:
        lo, hi, n, pnl, roi = best_e
        print(f"  лучший вход: кэф {lo:.2f}–{hi:.2f}  n={n}  pnl={pnl:.1f}  ROI={roi*100:.1f}%")

    # heat: prematch x entry for fav
    print("\nfav_s1: прематч × вход (ROI / n)")
    pm_b = [(1.01, 1.25), (1.25, 1.50), (1.50, 2.00)]
    en_b = [(1.01, 1.15), (1.15, 1.30), (1.30, 1.60), (1.60, 2.50)]
    print("  pm\\\\entry ", " ".join(f"{a:.2f}-{b:.2f}" for a, b in en_b))
    for plo, phi in pm_b:
        cells = []
        for elo, ehi in en_b:
            g = [t for t in fav if plo <= t["bought_odds"] < phi and elo <= t["entry_odds"] < ehi]
            if len(g) < 3:
                cells.append(f"{'·':>10}")
            else:
                stake = sum(t["stake"] for t in g)
                roi = sum(t["pnl"] for t in g) / stake
                cells.append(f"{roi*100:+5.1f}%/{len(g):<2d}")
        print(f"  {plo:.2f}-{phi:.2f} " + " ".join(f"{c:>10}" for c in cells))


if __name__ == "__main__":
    main()
