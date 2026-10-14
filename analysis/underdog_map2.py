"""BO3 map 2 scenarios: side (favorite / underdog by match price at scheduled start) x map 1 result (won / lost)
x match price bucket x tier -> buy that side in the map 2 market 2 min before map 2 begins (PandaScore timing,
after map 1 ended — no look-ahead).
Price: minute history (+ --slippage) and executable — VWAP of the first $100 of taker trades in the next 180 s
(analysis/executable.py). Halves = first / second half of the period by time (stability check).

  python analysis/underdog_map2.py [--sport dota2] [--slippage 0.01] [--min-n 30]
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmbot import storage  # noqa: E402
from pmbot.join import norm_team  # noqa: E402
from pmbot.tiers import tier_of  # noqa: E402
from between_maps import BEFORE, load_bo3, price_point  # noqa: E402
from executable import buys, vwap  # noqa: E402
from halftime_price import ps_windows  # noqa: E402

WINDOW, STAKE = 180, 100.0
BUCKETS = [(0.0, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.0)]


def collect(conn, sport: str, after_map1: int | None = None) -> list[dict]:
    """One row per (match, side): both teams of every BO3 with map 2 timing. Entry 2 min before map 2, or
    `after_map1` s after map 1 ended (the moment the live feed switches to map 2)."""
    out = []
    for m in load_bo3(conn):
        start = int(dt.datetime.fromisoformat(m["start"].replace("Z", "+00:00")).timestamp())
        pre = price_point(conn, m["ml_token"], start)
        if not pre or not 0.01 < pre[0] < 0.99:
            continue
        windows = ps_windows(conn, m["game"])
        if 1 not in windows or 2 not in windows or windows[2][0] - BEFORE <= windows[1][1] + 60:
            continue
        t = windows[2][0] - BEFORE if after_map1 is None else windows[1][1] + after_map1
        oa, token2, _ = m["maps"][2]
        cond, tok_a, tok_b = conn.execute(
            "SELECT condition_id, token_a, token_b FROM pm_markets WHERE event_id = ? AND token_a = ?",
            (m["event"], token2)).fetchone()
        pt = price_point(conn, tok_a, t)
        if not pt:
            continue
        a_first = norm_team(oa) == norm_team(m["a"])
        tier = "tier1" if tier_of(m["tournament"], sport) == "tier1" else "tier2+"
        for side in ("A", "B"):
            p0 = pre[0] if side == "A" else 1 - pre[0]
            mine = a_first == (side == "A")
            token, other = (tok_a, tok_b) if mine else (tok_b, tok_a)
            tape = buys(conn, {"cond": cond, "token": token, "token_other": other}, t, WINDOW)
            out.append({"t": t, "p0": p0, "p2": pt[0] if mine else 1 - pt[0], "won": m["winners"][1] == side,
                        "map1": "won" if m["winners"][0] == side else "lost", "tier": tier,
                        "role": "favorite" if p0 > 0.5 else "underdog",
                        "exec": vwap(tape, STAKE) if tape else None})
    out.sort(key=lambda x: x["t"])
    return out


def roi(items: list[tuple[float, bool]]) -> tuple[float, float]:
    rets = [(1 / (p + 0.05 * p * (1 - p)) if w else 0) - 1 for p, w in items]
    n = len(rets)
    mean = sum(rets) / n
    return mean, (sum((r - mean) ** 2 for r in rets) / max(n - 1, 1) / n) ** 0.5


def line(label: str, sel: list[dict], slippage: float) -> str:
    n = len(sel)
    hist = [(r["p2"] + slippage, r["won"]) for r in sel]
    m, se = roi(hist)
    half = n // 2
    h1, h2 = roi(hist[:half])[0], roi(hist[half:])[0]
    ex = [(r["exec"], r["won"]) for r in sel if r["exec"]]
    exs = f"{roi(ex)[0]:+6.1%} ({len(ex) / n:3.0%} filled)" if len(ex) >= 15 else f"{'-':>6} ({len(ex) / n:3.0%} filled)"
    return (f"  {label:34} n {n:4d}  map2 price {sum(r['p2'] for r in sel) / n:.3f}  win "
            f"{sum(r['won'] for r in sel) / n:6.1%}  ROI {m:+6.1%}±{se:4.1%}  halves {h1:+6.1%} {h2:+6.1%}  "
            f"real trades {exs}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="dota2", choices=storage.SPORTS)
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--after-map1", type=int, help="enter N s after map 1 ended instead of 2 min before map 2")
    args = ap.parse_args()

    rows = collect(storage.connect(storage.db_path(args.sport)), args.sport, args.after_map1)
    entry = "2 min before map 2" if args.after_map1 is None else f"{args.after_map1} s after map 1 ended"
    print(f"{args.sport} BO3: buy a side in the map 2 market {entry}; ROI after taker fee "
          f"(minute price +{args.slippage:.2f}; real trades = VWAP of first ${STAKE:.0f} in {WINDOW} s)\n")
    for tier in ("tier1", "tier2+", "all"):
        print(tier)
        for role in ("favorite", "underdog"):
            for map1 in ("lost", "won"):
                base = [r for r in rows if r["role"] == role and r["map1"] == map1
                        and (tier == "all" or r["tier"] == tier)]
                if len(base) < args.min_n:
                    continue
                print(line(f"{role} {map1} map 1, all", base, args.slippage))
                for lo, hi in BUCKETS:
                    sel = [r for r in base if lo <= r["p0"] < hi]
                    if len(sel) >= args.min_n:
                        print(line(f"   match price {lo:.2f}-{hi:.2f}", sel, args.slippage))
        print()


if __name__ == "__main__":
    main()
