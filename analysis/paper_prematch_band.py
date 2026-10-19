"""Paper live_copy: ROI by prematch / entry price for filled trades.

  python analysis/paper_prematch_band.py [--db data/live_copy.sqlite]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path


def buckets(step: float = 0.05, lo: float = 0.40, hi: float = 0.95) -> list[tuple[float, float]]:
    out = []
    x = lo
    while x < hi - 1e-9:
        out.append((round(x, 2), round(x + step, 2)))
        x += step
    return out


def sliding(step: float = 0.05, width: float = 0.15, lo: float = 0.40, hi: float = 0.95):
    out = []
    x = lo
    while x + width <= hi + 1e-9:
        out.append((round(x, 2), round(x + width, 2)))
        x += step
    return out


def load_trades(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT t.strategy, t.outcome, t.state, t.avg_price, t.best_ask, t.stake, t.fee,
                  t.pnl, t.status, t.filled, g.home, g.away, g.prematch_home
           FROM paper_trades t
           LEFT JOIN live_games g ON g.game_id = t.game_id
           WHERE t.status IN ('settled', 'open') AND t.filled > 0 AND t.avg_price IS NOT NULL"""
    ).fetchall()
    out = []
    for (strategy, outcome, state, avg_price, best_ask, stake, fee, pnl, status, filled,
         home, away, prematch_home) in rows:
        st = json.loads(state or "{}")
        pm = dict(st.get("prematch") or {})
        if not pm and prematch_home is not None and home and away:
            try:
                ph = float(prematch_home)
                pm = {home: ph, away: 1.0 - ph}
            except (TypeError, ValueError):
                pm = {}
        if outcome not in pm:
            continue
        bought_pm = float(pm[outcome])
        fav_pm = max(pm.values()) if pm else None
        out.append({
            "strategy": strategy,
            "outcome": outcome,
            "bought_pm": bought_pm,
            "fav_pm": fav_pm,
            "avg_price": float(avg_price),
            "best_ask": float(best_ask) if best_ask is not None else float(avg_price),
            "stake": float(stake or 0),
            "fee": float(fee or 0),
            "pnl": float(pnl) if pnl is not None else None,
            "status": status,
            "settled": status == "settled" and pnl is not None,
        })
    return out


def summarize(trades: list[dict], key: str, bands: list[tuple[float, float]], min_n: int = 5):
    """Return rows (lo, hi, n, wins, losses, stake, pnl, roi, wr) for settled trades."""
    settled = [t for t in trades if t["settled"]]
    rows = []
    for lo, hi in bands:
        g = [t for t in settled if lo <= t[key] < hi]
        if len(g) < min_n:
            continue
        n = len(g)
        wins = sum(1 for t in g if t["pnl"] > 0)
        losses = sum(1 for t in g if t["pnl"] <= 0)
        stake = sum(t["stake"] for t in g)
        pnl = sum(t["pnl"] for t in g)
        roi = pnl / stake if stake else 0.0
        rows.append((lo, hi, n, wins, losses, stake, pnl, roi, wins / n if n else 0.0))
    return rows


def print_table(title: str, rows: list, top: int | None = None):
    print(f"\n{title}")
    if not rows:
        print("  (нет бакетов с n>=min)")
        return
    show = sorted(rows, key=lambda r: -r[7])
    if top:
        show = show[:top]
    print(f"  {'band':>11} {'n':>4} {'W-L':>7} {'stake':>8} {'pnl':>9} {'ROI':>7} {'WR':>6}")
    for lo, hi, n, w, l, stake, pnl, roi, wr in show:
        print(f"  {lo:.2f}-{hi:.2f} {n:4d} {w:3d}-{l:<3d} {stake:8.0f} {pnl:9.1f} {roi*100:6.1f}% {wr*100:5.1f}%")


def best_contiguous(trades: list[dict], key: str, step: float = 0.05, min_n: int = 15):
    """Brute-force best [lo,hi) on a 0.05 grid by ROI, with n>=min_n."""
    settled = [t for t in trades if t["settled"]]
    vals = sorted({round(x * 20) / 20 for x in [0.40 + i * step for i in range(12)]})
    best = None
    for i, lo in enumerate(vals):
        for hi in vals[i + 1:]:
            g = [t for t in settled if lo <= t[key] < hi]
            if len(g) < min_n:
                continue
            stake = sum(t["stake"] for t in g)
            pnl = sum(t["pnl"] for t in g)
            if not stake:
                continue
            roi = pnl / stake
            rec = (lo, hi, len(g), stake, pnl, roi)
            if best is None or roi > best[5]:
                best = rec
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/live_copy.sqlite")
    ap.add_argument("--min-n", type=int, default=5)
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        sys.exit(f"no db: {path}")

    conn = sqlite3.connect(path)
    trades = load_trades(conn)
    settled = [t for t in trades if t["settled"]]
    print(f"db={path}  filled={len(trades)}  settled={len(settled)}")

    by_s: dict[str, list] = defaultdict(list)
    for t in trades:
        by_s[t["strategy"]].append(t)

    # overall by strategy briefly
    print("\n=== по стратегиям (settled) ===")
    print(f"  {'strategy':<22} {'n':>4} {'pnl':>9} {'ROI':>7}")
    for s in sorted(by_s):
        g = [t for t in by_s[s] if t["settled"]]
        if not g:
            continue
        stake = sum(t["stake"] for t in g)
        pnl = sum(t["pnl"] for t in g)
        print(f"  {s:<22} {len(g):4d} {pnl:9.1f} {pnl/stake*100:6.1f}%")

    focus = ["leader_half6", "leader_half", "fav_band_t1", "maker_t1",
             "leader_late", "fav_comeback_half", "fav_comeback_wide"]
    # also all leaders combined
    leaders = by_s["leader_half"] + by_s["leader_half6"]

    groups = [(s, by_s[s]) for s in focus if by_s[s]]
    groups.append(("leader_half+half6", leaders))

    fixed = buckets(0.05, 0.45, 0.95)
    windows = sliding(0.05, 0.15, 0.45, 0.95)

    for name, g in groups:
        print(f"\n{'='*60}\n{name}  n_settled={sum(1 for t in g if t['settled'])}")
        # prematch of bought side
        print_table("прематч купленной стороны (шаг 0.05)", summarize(g, "bought_pm", fixed, args.min_n))
        print_table("прематч купленной — окна 0.15 (топ по ROI)", summarize(g, "bought_pm", windows, args.min_n), top=8)
        b = best_contiguous(g, "bought_pm", min_n=max(args.min_n, 12))
        if b:
            lo, hi, n, stake, pnl, roi = b
            print(f"  >> лучший contiguous прематч: {lo:.2f}-{hi:.2f}  n={n}  pnl={pnl:.1f}  ROI={roi*100:.1f}%")

        # match favorite prematch (context)
        print_table("прематч фаворита матча (шаг 0.05)", summarize(g, "fav_pm", fixed, args.min_n))

        # entry price
        print_table("цена входа avg_price (шаг 0.05)", summarize(g, "avg_price", fixed, args.min_n))
        print_table("цена входа — окна 0.15 (топ)", summarize(g, "avg_price", windows, args.min_n), top=8)
        b2 = best_contiguous(g, "avg_price", min_n=max(args.min_n, 12))
        if b2:
            lo, hi, n, stake, pnl, roi = b2
            print(f"  >> лучший contiguous вход: {lo:.2f}-{hi:.2f}  n={n}  pnl={pnl:.1f}  ROI={roi*100:.1f}%")

    # cross: leaders only, heatmap-ish prematch x entry
    print(f"\n{'='*60}\nleader_half+half6: прематч купленного × цена входа")
    pm_bands = [(0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 0.95)]
    en_bands = [(0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 0.95)]
    hdr = "pm\\entry"
    print(f"  {hdr:>12}", " ".join(f"{a:.2f}-{b:.2f}" for a, b in en_bands))
    for plo, phi in pm_bands:
        cells = []
        for elo, ehi in en_bands:
            g = [t for t in leaders if t["settled"]
                 and plo <= t["bought_pm"] < phi and elo <= t["avg_price"] < ehi]
            if len(g) < 3:
                cells.append(f"{'·':>9}")
            else:
                stake = sum(t["stake"] for t in g)
                roi = sum(t["pnl"] for t in g) / stake
                cells.append(f"{roi*100:+5.1f}%/{len(g):<2d}")
        print(f"  {plo:.2f}-{phi:.2f}  " + " ".join(f"{c:>9}" for c in cells))


if __name__ == "__main__":
    main()
