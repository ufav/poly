"""Back the top-N team (Valve VRS ranking as of the match date) against a team outside the top-N,
match-winner market, at the price at scheduled start. Staking: flat % of bank, x2 after a loss (literal martingale),
and a "full chase" that sizes the next bet to win back the chain losses plus the normal profit.

Rankings: monthly snapshots of github.com/ValveSoftware/counter-strike_regional_standings (live/<year>/
standings_global_<date>.md) saved to data/vrs/<YYYY_MM_DD>.md. HLTV blocks scripted access (403).
Bets are settled one by one in start-time order (parallel matches are treated as sequential).
--teams replaces the dated ranking with a fixed list (e.g. today's HLTV top-10; look-ahead on past data).
Ruin risk: the same bets in --shuffles random orders, share of runs ending below 10% of the bank.

  python analysis/top10_martingale.py [--top 10] [--teams "Spirit,Vitality,..."] [--stake 0.02] [--bank 1000]
                                      [--amount 100]
"""

import argparse
import datetime as dt
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402

VRS_DIR = Path(__file__).resolve().parents[1] / "data" / "vrs"
ALIASES = {"Aurora": "Aurora Gaming", "BetBoom": "BetBoom Team", "FUT": "FUT Esports",
           "Falcons": "Team Falcons", "The MongolZ": "TheMongolz"}
BUCKETS = [(0.0, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.0)]


def rankings(top: int) -> list[tuple[int, set[str]]]:
    """[(snapshot unix time, Polymarket names of the top-N)] sorted by time."""
    out = []
    for f in sorted(VRS_DIR.glob("*.md")):
        t = int(dt.datetime.strptime(f.stem, "%Y_%m_%d").replace(tzinfo=dt.timezone.utc).timestamp())
        names = set()
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\|\s*(\d+)\s*\|\s*-?\d+\s*\|\s*([^|]+?)\s*\|", line)
            if m and int(m.group(1)) <= top:
                names.add(ALIASES.get(m.group(2), m.group(2)))
        out.append((t, names))
    return out


def bets(conn, ranks, slippage: float) -> list[dict]:
    rows = conn.execute(
        """SELECT m.outcome_a, m.outcome_b, m.token_a, m.token_b, m.final_price_a, e.start_time, e.tournament, e.best_of
           FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
           WHERE m.market_type = 'moneyline' AND m.history_status = 'done' AND m.closed = 1
             AND m.final_price_a IN (0, 1)""").fetchall()
    out = []
    for a, b, tok_a, tok_b, final_a, start_time, tournament, bo in rows:
        start = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
        snap = [names for t, names in ranks if t <= start]
        if not snap:
            continue
        top = snap[-1]
        if (a in top) == (b in top):
            continue
        team, opp, token, won = (a, b, tok_a, final_a == 1) if a in top else (b, a, tok_b, final_a == 0)
        pre = conn.execute("SELECT p FROM pm_prices WHERE token_id = ? AND t <= ? ORDER BY t DESC LIMIT 1",
                           (token, start)).fetchone()
        if not pre or not 0.02 < pre[0] < 0.98:
            continue
        p = pre[0]
        out.append({"t": start, "team": team, "opp": opp, "p": p, "won": won, "bo": bo, "tournament": tournament,
                    "cost": p + slippage + 0.05 * p * (1 - p)})
    out.sort(key=lambda x: x["t"])
    return out


def simulate(items: list[dict], bank0: float, stake: float, mode: str) -> dict:
    """mode: flat | x2 | chase. Returns final bank, max drawdown, max stake share, bust flag."""
    bank, peak, max_dd, max_share, base, lost, k = bank0, bank0, 0.0, 0.0, 0.0, 0.0, 0
    for x in items:
        net = 1 / x["cost"] - 1
        if mode == "flat" or k == 0:
            base = stake * bank
        if mode == "flat" or k == 0:
            bet = base
        elif mode == "x2":
            bet = base * 2 ** k
        else:
            bet = (lost + base * net) / net
        bet = min(bet, bank)
        max_share = max(max_share, bet / bank)
        if x["won"]:
            bank += bet * net
            k, lost = 0, 0.0
        else:
            bank -= bet
            k, lost = k + 1, lost + bet
        peak = max(peak, bank)
        max_dd = max(max_dd, 1 - bank / peak)
        if bank < 1:
            return {"bank": bank, "dd": 1.0, "share": max_share, "bust": x["t"]}
    return {"bank": bank, "dd": max_dd, "share": max_share, "bust": None}


def fixed(items: list[dict], amount: float) -> tuple[float, float, float]:
    """Same $ on every bet: total P&L, max drawdown from the running peak, lowest point ($)."""
    pnl, peak, max_dd, low = 0.0, 0.0, 0.0, 0.0
    for x in items:
        pnl += amount * (1 / x["cost"] - 1) if x["won"] else -amount
        peak = max(peak, pnl)
        max_dd = max(max_dd, peak - pnl)
        low = min(low, pnl)
    return pnl, max_dd, low


def streaks(items: list[dict]) -> dict[int, int]:
    out, k = defaultdict(int), 0
    for x in items:
        if x["won"]:
            if k:
                out[k] += 1
            k = 0
        else:
            k += 1
    if k:
        out[k] += 1
    return out


def summary(items: list[dict]) -> str:
    n = len(items)
    w = sum(x["won"] for x in items)
    rets = [(1 / x["cost"] if x["won"] else 0) - 1 for x in items]
    mean = sum(rets) / n
    se = (sum((r - mean) ** 2 for r in rets) / max(n - 1, 1) / n) ** 0.5
    avg_p = sum(x["p"] for x in items) / n
    return f"n {n:4d}  win {w / n:6.1%}  avg price {avg_p:.3f}  edge {w / n - avg_p:+.3f}  flat ROI {mean:+6.1%}±{se:.1%}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--stake", type=float, default=0.02)
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--bank", type=float, default=1000)
    ap.add_argument("--teams", help="comma-separated Polymarket team names, fixed for the whole period")
    ap.add_argument("--shuffles", type=int, default=2000)
    ap.add_argument("--amount", type=float, default=100, help="fixed $ per bet")
    args = ap.parse_args()

    conn = storage.connect()
    if args.teams:
        ranks = [(0, {t.strip() for t in args.teams.split(",")})]
        label = "fixed list"
    else:
        ranks = rankings(args.top)
        label = f"top-{args.top} (VRS as of match date)"
    items = bets(conn, ranks, args.slippage)
    if not items:
        print("no bets")
        return
    first = dt.datetime.fromtimestamp(items[0]["t"], dt.timezone.utc).date()
    last = dt.datetime.fromtimestamp(items[-1]["t"], dt.timezone.utc).date()
    print(f"{label} vs teams outside it, match winner, price at start + taker fee + "
          f"{args.slippage:.2f} slippage | {first} .. {last}\n")
    print("ALL      ", summary(items))
    for lo, hi in BUCKETS:
        sel = [x for x in items if lo <= x["p"] < hi]
        if sel:
            print(f"{lo:.2f}-{hi:.2f}", summary(sel))
    for bo in (1, 3, 5):
        sel = [x for x in items if x["bo"] == bo]
        if sel:
            print(f"BO{bo}      ", summary(sel))

    print("\nby month")
    months = defaultdict(list)
    for x in items:
        months[dt.datetime.fromtimestamp(x["t"], dt.timezone.utc).strftime("%Y-%m")].append(x)
    for m, sel in sorted(months.items()):
        print(f"  {m}  ", summary(sel))

    print("\nby team")
    teams = defaultdict(list)
    for x in items:
        teams[x["team"]].append(x)
    for team, sel in sorted(teams.items(), key=lambda kv: -len(kv[1])):
        if len(sel) >= 10:
            print(f"  {team:16}", summary(sel))

    s = streaks(items)
    print("\nlosing streaks (length: count):", ", ".join(f"{k}: {v}" for k, v in sorted(s.items())))

    print(f"\nfixed ${args.amount:,.0f} per bet")
    for label, sel in (("all", items), ("price < 0.80", [x for x in items if x["p"] < 0.80]),
                       ("price >= 0.80", [x for x in items if x["p"] >= 0.80])):
        pnl, dd, low = fixed(sel, args.amount)
        print(f"  {label:13} bets {len(sel):4d}  turnover ${len(sel) * args.amount:>8,.0f}  P&L ${pnl:>+8,.0f}  "
              f"max drawdown ${dd:,.0f}  lowest ${low:+,.0f}")

    print(f"\nbank ${args.bank:,.0f}, base stake {args.stake:.0%} of bank")
    for label, sel in (("all", items), ("price < 0.80", [x for x in items if x["p"] < 0.80]),
                       ("price >= 0.80", [x for x in items if x["p"] >= 0.80])):
        for mode in ("flat", "x2", "chase"):
            r = simulate(sel, args.bank, args.stake, mode)
            bust = f"  BUST {dt.datetime.fromtimestamp(r['bust'], dt.timezone.utc).date()}" if r["bust"] else ""
            rng = random.Random(1)
            finals = []
            for _ in range(args.shuffles):
                order = sel[:]
                rng.shuffle(order)
                finals.append(simulate(order, args.bank, args.stake, mode)["bank"])
            finals.sort()
            ruin = sum(f < 0.1 * args.bank for f in finals) / len(finals)
            print(f"  {label:13} {mode:5}  final ${r['bank']:>9,.0f}  max drawdown {r['dd']:5.1%}  "
                  f"max bet {r['share']:5.1%} of bank{bust} | shuffled: median ${finals[len(finals) // 2]:,.0f}  "
                  f"ruin {ruin:.1%}")


if __name__ == "__main__":
    main()
