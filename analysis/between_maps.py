"""BO3 between maps: does the market overreact to the previous map?

A-28. Underdog (match price at start <= 1 - --fav) won map 1 -> at map 2 start: underdog's match price and
      map 2 price vs results (and the normal case, favorite won map 1, for comparison).
A-29. 1:1 -> at map 3 start: price of the map 2 winner ("momentum") vs map 3 result.

Map winners from settled Polymarket map markets; prices taken 2 min before map begin (PandaScore), after the
previous map ended — no look-ahead. ROI: taker fee 0.05*p*(1-p) + --slippage.

  python analysis/between_maps.py [--fav 0.60] [--slippage 0.01] [--sport dota2]
"""

import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmbot import storage  # noqa: E402
from pmbot.join import norm_team  # noqa: E402
from halftime_price import ps_windows  # noqa: E402
from stage_calibration import tier_of  # noqa: E402

BEFORE = 120


def price_point(conn, token: str, t: int) -> tuple[float, int] | None:
    """(last price at or before t, its age in seconds)."""
    row = conn.execute("SELECT p, t FROM pm_prices WHERE token_id = ? AND t <= ? ORDER BY t DESC LIMIT 1",
                       (token, t)).fetchone()
    return (row[0], t - row[1]) if row else None


def load_bo3(conn) -> list[dict]:
    out = []
    events = conn.execute(
        """SELECT id, game_id, start_time, tournament FROM pm_events
           WHERE best_of = 3 AND game_id IS NOT NULL AND start_time IS NOT NULL""").fetchall()
    for event_id, game_id, start_time, tournament in events:
        ml, maps = None, {}
        ml_volume = 0
        for mtype, num, oa, ob, token, final, volume in conn.execute(
                """SELECT market_type, map_number, outcome_a, outcome_b, token_a, final_price_a, volume FROM pm_markets
                   WHERE event_id = ? AND history_status = 'done'""", (event_id,)):
            if mtype == "moneyline":
                ml = (oa, ob, token, final)
                ml_volume = volume or 0
            elif mtype == "child_moneyline" and num in (1, 2, 3):
                maps[num] = (oa, token, final)
        if not ml or ml[3] is None or 1 not in maps or 2 not in maps:
            continue
        a, b = ml[0], ml[1]
        winners = []
        for num in (1, 2):
            m = maps.get(num)
            if not m or m[2] is None or 0.05 < m[2] < 0.95:
                break
            a_won = (m[2] >= 0.95) == (norm_team(m[0]) == norm_team(a))
            winners.append("A" if a_won else "B")
            if winners.count("A") == 2 or winners.count("B") == 2:
                break
        if len(winners) < 2 or not (ml[3] >= 0.95 or ml[3] <= 0.05):
            continue
        match_winner = "A" if ml[3] >= 0.95 else "B"
        if winners[0] != winners[1]:
            winners.append(match_winner)  # no map 3 market on Polymarket: at 1:1 map 3 = match
        out.append({"event": event_id, "game": game_id, "start": start_time, "tournament": tournament,
                    "a": a, "ml_token": ml[2], "match_winner": match_winner, "maps": maps, "winners": winners[:3],
                    "ml_volume": ml_volume})
    return out


def report(label: str, items: list[tuple[float, bool]], slippage: float) -> None:
    if len(items) < 30:
        print(f"   {label:52} n={len(items):4d}  (too few)")
        return
    n = len(items)
    win = sum(w for _, w in items) / n
    price = sum(p for p, _ in items) / n
    cost = sum(p + slippage + 0.05 * p * (1 - p) for p, _ in items) / n
    se2 = 2 * (win * (1 - win) / n) ** 0.5
    print(f"   {label:52} n={n:4d}  price {price:.3f}  win {win:6.1%} ±{se2:4.1%}  edge {win - price:+.3f}"
          f"  ROI {(win - cost) / cost:+6.1%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fav", type=float, default=0.60)
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--sport", choices=storage.SPORTS, default="cs2")
    args = ap.parse_args()
    conn = storage.connect(storage.db_path(args.sport))
    matches = load_bo3(conn)
    print(f"BO3 matches with settled maps 1-2: {len(matches)}\n")

    g28 = defaultdict(list)
    g29 = defaultdict(list)
    for m in matches:
        windows = ps_windows(conn, m["game"])
        start_ts = int(dt.datetime.fromisoformat(m["start"].replace("Z", "+00:00")).timestamp())
        pre = price_point(conn, m["ml_token"], start_ts)
        if pre is None:
            continue
        pre_a = pre[0]
        fav = "A" if pre_a >= 0.5 else "B"
        dog = "B" if fav == "A" else "A"
        fav_p = max(pre_a, 1 - pre_a)
        strong = fav_p >= args.fav
        w = m["winners"]

        def side_price(p_a: float, side: str) -> float:
            return p_a if side == "A" else 1 - p_a

        if 2 in windows and 1 in windows and windows[2][0] - BEFORE > windows[1][1] + 60:
            t = windows[2][0] - BEFORE
            ml_pt = price_point(conn, m["ml_token"], t)
            oa, token2, _ = m["maps"][2]
            map2_pt = price_point(conn, token2, t)
            if strong and ml_pt and map2_pt:
                ml_a = ml_pt[0]
                map2_a = map2_pt[0] if norm_team(oa) == norm_team(m["a"]) else 1 - map2_pt[0]
                moved = conn.execute("SELECT COUNT(DISTINCT p) FROM pm_prices WHERE token_id = ? AND t BETWEEN ? AND ?",
                                     (token2, t - 3600, t)).fetchone()[0] > 1
                key = "underdog won map 1" if w[0] == dog else "favorite won map 1"
                bucket = ("0.50-0.60" if fav_p < 0.60 else "0.60-0.70" if fav_p < 0.70 else "0.70-0.80"
                          if fav_p < 0.80 else "0.80+")
                tier = "tier1" if tier_of(m["tournament"], args.sport) == "tier1" else "not tier1"
                vol = "match volume >= $200k" if m["ml_volume"] >= 200_000 else "match volume < $200k"
                for b in ("all", bucket, "map2 price moved in last hour" if moved else "map2 price flat for 1 h",
                          tier, vol):
                    g28[(key, "match", b)].append((side_price(ml_a, dog), m["match_winner"] == dog))
                    g28[(key, "map2", b)].append((side_price(map2_a, dog), w[1] == dog))

        if len(w) == 3 and 3 in windows and 2 in windows and windows[3][0] - BEFORE > windows[2][1] + 60:
            t = windows[3][0] - BEFORE
            pt = price_point(conn, m["ml_token"], t)
            if pt is None:
                continue
            p3_a = pt[0]
            mom = w[1]
            who = "favorite" if mom == fav else "underdog"
            item = (side_price(p3_a, mom), w[2] == mom)
            g29["all"].append(item)
            g29["tier1" if tier_of(m["tournament"], args.sport) == "tier1" else "not tier1"].append(item)
            g29[f"map 2 won by the {who}"].append(item)
            if strong:
                g29[f"map 2 won by the {who} (fav >= {args.fav:.2f})"].append(item)

    print(f"A-28. Favorite >= {args.fav:.2f}; buy the UNDERDOG at map 2 start (2 min before)")
    for key in ("underdog won map 1", "favorite won map 1"):
        for mk in ("match", "map2"):
            for bucket in ("all", "0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80+", "map2 price moved in last hour",
                           "map2 price flat for 1 h", "tier1", "not tier1", "match volume >= $200k",
                           "match volume < $200k"):
                report(f"{key}: {mk} market, fav {bucket}", g28[(key, mk, bucket)], args.slippage)
        print()

    print("A-29. 1:1, buy the MAP 2 WINNER at map 3 start (2 min before); edge < 0 = momentum overpriced")
    for key in sorted(g29):
        report(key, g29[key], args.slippage)


if __name__ == "__main__":
    main()
