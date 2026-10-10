"""Polymarket map price at halftime (estimated, see pmbot/maptiming.py) vs actual map result.

1. Timing sanity: minutes per round, leader price by lead size.
2. Leader after 12 rounds: price vs win rate by lead and first-half side — does the market price the side swap?
3. User scenario: pre-match favorite (match price >= --fav) trails at halftime — price vs win rate,
   net of taker fee 0.05*p*(1-p) and --slippage.

  python analysis/halftime_price.py [--fav 0.70] [--entry-delay 60] [--slippage 0.01]
"""

import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402
from pmbot.join import norm_team  # noqa: E402
from pmbot.maptiming import map_window, price_at, price_series  # noqa: E402
from stage_calibration import tier_of  # noqa: E402

MIN_PER_ROUND = (1.3, 3.2)


def load_rounds(conn, game_id: str) -> list[tuple]:
    return conn.execute(
        "SELECT round, ct_team, t_team, winner_side, winner_team FROM cs_rounds WHERE game_id = ? ORDER BY round",
        (game_id,)).fetchall()


def collect(conn, entry_delays: tuple[int, ...], windows: list | None = None) -> tuple[list[dict], list[float]]:
    """Halftime rows per team; if `windows` is given, also appends every timed map:
    {"path": [(t, price of first-half CT team)], "t0", "t1", "rounds", "ct_won", "date"}."""
    rows = []
    per_round = []
    matches = conn.execute(
        """SELECT c.id, e.id, e.start_time, e.best_of, e.tournament FROM cs_matches c
           JOIN pm_events e ON e.game_id = c.external_id WHERE c.stats_status = 'done'""").fetchall()
    for cs_id, event_id, start_time, best_of, tournament in matches:
        markets = {}
        moneyline = None
        for mtype, num, token, outcome_a in conn.execute(
                """SELECT market_type, map_number, token_a, outcome_a FROM pm_markets
                   WHERE event_id = ? AND history_status = 'done'""", (event_id,)):
            if mtype == "child_moneyline" and num:
                markets[num] = (token, outcome_a)
            elif mtype == "moneyline":
                moneyline = (token, outcome_a)
        if not moneyline or not start_time:
            continue
        start_ts = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
        ml_series = price_series(conn, moneyline[0])
        pre = price_at(ml_series, start_ts)
        if pre is None:
            continue

        lower = start_ts - 600
        maps_won = defaultdict(int)
        for game_id, number, map_name in conn.execute(
                "SELECT id, number, map_name FROM cs_games WHERE match_id = ? ORDER BY number", (cs_id,)):
            rounds = load_rounds(conn, game_id)
            if len(rounds) < 13 or rounds[0][0] != 1:
                break
            wins = defaultdict(int)
            half = {}
            for rnd, ct, t, side, winner in rounds:
                wins[winner] += 1
                if rnd == 12:
                    half = dict(wins)
            winner = max(wins, key=wins.get)
            first_ct, first_t = rounds[0][1], rounds[0][2]

            deciding = best_of and all(v == (best_of - 1) // 2 for v in maps_won.values()) and len(maps_won) == 2
            market = markets.get(number) or (moneyline if deciding else None)
            maps_won[winner] += 1
            if not market:
                break
            token, outcome_a = market
            series = price_series(conn, token) if market is not moneyline else ml_series
            a_won = norm_team(outcome_a) == norm_team(winner)
            if not a_won and norm_team(outcome_a) not in (norm_team(first_ct), norm_team(first_t)):
                break
            window = map_window(series, lower, a_won)
            if not window:
                break
            t0, t1 = window
            lower = t1
            mpr = (t1 - t0) / 60 / len(rounds)
            per_round.append(mpr)
            if not MIN_PER_ROUND[0] <= mpr <= MIN_PER_ROUND[1]:
                continue
            a_name = first_ct if norm_team(outcome_a) == norm_team(first_ct) else first_t
            if windows is not None:
                windows.append({
                    "path": [(t, p if a_name == first_ct else 1 - p) for t, p in series if t0 <= t <= t1],
                    "t0": t0, "t1": t1, "rounds": len(rounds), "ct_won": winner == first_ct,
                    "date": start_time[:10],
                })
            t_half = t0 + (t1 - t0) * 12 / len(rounds)
            prices_a = {d: price_at(series, int(t_half + d)) for d in entry_delays}
            if any(p is None for p in prices_a.values()):
                continue
            ml_a = norm_team(moneyline[1])
            pre_by_team = {a: (pre if norm_team(a) == ml_a else 1 - pre) for a in (first_ct, first_t)}
            path_a = [(t, p) for t, p in series if t_half <= t <= t1]
            for team, other in ((first_ct, first_t), (first_t, first_ct)):
                rows.append({
                    "path": [(t, p if team == a_name else 1 - p) for t, p in path_a],
                    "t_half": t_half, "t_r18": t0 + (t1 - t0) * 18 / len(rounds),
                    "date": start_time[:10], "tier": tier_of(tournament),
                    "team": team, "map": map_name, "first_side": "CT" if team == first_ct else "T",
                    "lead": half.get(team, 0) - half.get(other, 0), "won": winner == team,
                    "pre": pre_by_team[team],
                    "price": {d: (p if team == a_name else 1 - p) for d, p in prices_a.items()},
                })
    return rows, per_round


def exit_rules(slippage: float) -> dict:
    """Exit functions: (row, entry time) -> sale proceeds per share, or None to hold to settlement."""
    def sell(p: float) -> float:
        return p - slippage - 0.05 * p * (1 - p)

    def take_profit(level: float):
        def fn(r, t_in):
            hit = next((p for t, p in r["path"] if t > t_in and p >= level), None)
            return sell(hit) if hit is not None else None
        return fn

    def stop_loss(level: float):
        def fn(r, t_in):
            hit = next((p for t, p in r["path"] if t > t_in and p <= level), None)
            return sell(hit) if hit is not None else None
        return fn

    def at_round_18(r, t_in):
        p = next((p for t, p in r["path"] if t >= r["t_r18"]), None)
        return sell(p) if p is not None and p < 0.99 else None

    def tp_and_sl(tp: float, sl: float):
        def fn(r, t_in):
            hit = next((p for t, p in r["path"] if t > t_in and (p >= tp or p <= sl)), None)
            return sell(hit) if hit is not None else None
        return fn

    return {
        "hold to the end of the map": lambda r, t: None,
        "take profit at 0.93": take_profit(0.93),
        "take profit at 0.95": take_profit(0.95),
        "take profit at 0.97": take_profit(0.97),
        "sell at ~round 18 (if still < 0.99)": at_round_18,
        "stop loss at 0.60": stop_loss(0.60),
        "stop loss at 0.50": stop_loss(0.50),
        "take profit 0.95 + stop loss 0.60": tp_and_sl(0.95, 0.60),
    }


def line(label: str, items: list[dict], delay: int, slippage: float) -> str:
    n = len(items)
    win = sum(r["won"] for r in items) / n
    price = sum(r["price"][delay] for r in items) / n
    cost = sum(r["price"][delay] + slippage + 0.05 * r["price"][delay] * (1 - r["price"][delay]) for r in items) / n
    se2 = 2 * (win * (1 - win) / n) ** 0.5
    return (f"   {label:34} n={n:4d}  price {price:5.3f}  win {win:6.1%} ±{se2:5.1%}  edge {win - price:+.3f}"
            f"  net {win - cost:+.3f}  ROI {(win - cost) / cost:+6.1%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fav", type=float, default=0.70)
    ap.add_argument("--entry-delay", type=int, default=60, help="seconds after estimated end of round 12")
    ap.add_argument("--slippage", type=float, default=0.01)
    args = ap.parse_args()
    delays = (-120, args.entry_delay, 180)
    conn = storage.connect()
    rows, per_round = collect(conn, delays)
    d = args.entry_delay

    print(f"maps with estimated timing: {len(per_round)}; median {median(per_round):.2f} min/round; "
          f"kept {len(rows) // 2} maps with {MIN_PER_ROUND[0]}-{MIN_PER_ROUND[1]} min/round\n")

    print(f"1. Sanity: price of the team leading after 12 rounds (entry = half +{d}s; also -120s / +180s)")
    by_lead = defaultdict(list)
    for r in rows:
        if r["lead"] > 0:
            by_lead[min(r["lead"], 8)].append(r)
    for lead, items in sorted(by_lead.items()):
        avg = {x: sum(r["price"][x] for r in items) / len(items) for x in delays}
        print(f"   lead +{lead}{'+' if lead == 8 else ' '} n={len(items):4d}  price at -120s {avg[-120]:.3f}  "
              f"+{d}s {avg[d]:.3f}  +180s {avg[180]:.3f}  win {sum(r['won'] for r in items) / len(items):.1%}")

    print("\n2. Leader after 12 rounds: price vs win rate by lead and first-half side")
    for lead in (2, 4, 6):
        for side in ("CT", "T"):
            items = [r for r in rows if r["lead"] == lead and r["first_side"] == side]
            if len(items) >= 20:
                print(line(f"lead +{lead}, leader was {side}", items, d, args.slippage))

    print(f"\n3. Favorite (pre-match >= {args.fav:.2f}) trails at halftime: buy the favorite")
    fav_rows = [r for r in rows if r["pre"] >= args.fav and r["lead"] < 0]
    groups = {
        "all": fav_rows,
        "underdog was CT (fav gets CT)": [r for r in fav_rows if r["first_side"] == "T"],
        "underdog was T (fav gets T)": [r for r in fav_rows if r["first_side"] == "CT"],
    }
    for deficit in (2, 4, 6):
        for side, label in (("T", "dog CT"), ("CT", "dog T")):
            groups[f"-{deficit}, {label}"] = [r for r in fav_rows if r["lead"] == -deficit and r["first_side"] == side]
    groups["-2..-4, dog CT"] = [r for r in fav_rows if -4 <= r["lead"] <= -2 and r["first_side"] == "T"]
    for label, items in groups.items():
        if len(items) >= 10:
            print(line(label, items, d, args.slippage))

    print("\n5. Rule 'buy the leader at halftime if lead >= 6, or lead 4 and leader was T': robustness")
    rule = [r for r in rows if r["lead"] >= 6 or (r["lead"] == 4 and r["first_side"] == "T")]
    splits = {
        "all": rule,
        "lead 8+": [r for r in rule if r["lead"] >= 8],
        "Sep 2025 - Mar 2026": [r for r in rule if r["date"] < "2026-04-01"],
        "Apr 2026 - Oct 2026": [r for r in rule if r["date"] >= "2026-04-01"],
        "tier1": [r for r in rule if r["tier"] == "tier1"],
        "not tier1": [r for r in rule if r["tier"] != "tier1"],
        "leader was pre-match favorite": [r for r in rule if r["pre"] >= 0.5],
        "leader was pre-match underdog": [r for r in rule if r["pre"] < 0.5],
    }
    for label, items in splits.items():
        if len(items) >= 10:
            print(line(label, items, d, args.slippage))

    print("\n6. Same rule, exit variants (sell = price - slippage - taker fee; hold = $1 / $0 at settlement)")
    for label, exit_fn in exit_rules(args.slippage).items():
        pnl, cost, early = 0.0, 0.0, 0
        for r in rule:
            entry = r["price"][d] + args.slippage
            fee_in = 0.05 * r["price"][d] * (1 - r["price"][d])
            proceeds = exit_fn(r, r["t_half"] + d)
            if proceeds is None:
                proceeds = 1.0 if r["won"] else 0.0
            else:
                early += 1
            pnl += proceeds - entry - fee_in
            cost += entry + fee_in
        print(f"   {label:40} n={len(rule)}  sold early {early:4d}  profit/trade {pnl / len(rule):+.4f}  ROI {pnl / cost:+6.2%}")

    print(f"\n4. Mirror: underdog (pre-match <= {1 - args.fav:.2f}) leads at halftime: buy the underdog")
    dog_rows = [r for r in rows if r["pre"] <= 1 - args.fav and r["lead"] > 0]
    groups = {"all": dog_rows}
    for lead in (2, 4, 6):
        for side in ("CT", "T"):
            groups[f"+{lead}, dog was {side}"] = [r for r in dog_rows if r["lead"] == lead and r["first_side"] == side]
    for label, items in groups.items():
        if len(items) >= 10:
            print(line(label, items, d, args.slippage))


if __name__ == "__main__":
    main()
