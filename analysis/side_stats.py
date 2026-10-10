"""CS2 side and map statistics from cybersport.ru rounds (no prices):

1. CT round win rate per map (regulation rounds 1-24).
2. Map win probability for the team leading after the first half, by lead size and first-half side.
3. User scenario: pre-match favorite (Polymarket match price at start >= --fav) trails after the first half
   while the underdog played CT — how often the favorite still wins the map, per map.

  python analysis/side_stats.py [--fav 0.70]
"""

import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402


def load_maps(conn) -> list[dict]:
    games = conn.execute(
        """SELECT g.id, g.map_name, c.external_id FROM cs_games g JOIN cs_matches c ON c.id = g.match_id
           WHERE g.rounds >= 13"""
    ).fetchall()
    result = []
    for game_id, map_name, external_id in games:
        rounds = conn.execute(
            "SELECT round, ct_team, t_team, winner_side, winner_team FROM cs_rounds WHERE game_id = ? ORDER BY round",
            (game_id,),
        ).fetchall()
        if len(rounds) < 13 or rounds[0][0] != 1:
            continue
        wins = defaultdict(int)
        half = None
        for rnd, ct, t, side, winner in rounds:
            wins[winner] += 1
            if rnd == 12:
                half = dict(wins)
        teams = {rounds[0][1], rounds[0][2]}
        if half is None or len(teams) != 2:
            continue
        map_winner = max(wins, key=wins.get)
        result.append({
            "game_id": game_id, "map": map_name, "external_id": external_id, "rounds": rounds,
            "first_ct": rounds[0][1], "first_t": rounds[0][2], "half": half, "winner": map_winner,
        })
    return result


def pm_favorite(conn, external_id: int | None, fav: float) -> tuple[str, float] | None:
    if external_id is None:
        return None
    row = conn.execute(
        """SELECT e.start_time, m.token_a, m.outcome_a, m.outcome_b FROM pm_events e
           JOIN pm_markets m ON m.event_id = e.id AND m.market_type = 'moneyline'
           WHERE e.game_id = ? AND m.history_status = 'done'""",
        (external_id,),
    ).fetchone()
    if not row:
        return None
    start = int(dt.datetime.fromisoformat(row[0].replace("Z", "+00:00")).timestamp())
    p = conn.execute("SELECT p FROM pm_prices WHERE token_id = ? AND t <= ? ORDER BY t DESC LIMIT 1",
                     (row[1], start)).fetchone()
    if not p:
        return None
    if p[0] >= fav:
        return row[2], p[0]
    if 1 - p[0] >= fav:
        return row[3], 1 - p[0]
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fav", type=float, default=0.70)
    args = ap.parse_args()
    conn = storage.connect()
    maps = load_maps(conn)
    print(f"maps with round data: {len(maps)}\n")

    print("1. CT round win rate, rounds 1-24")
    per_map = defaultdict(lambda: [0, 0])
    for m in maps:
        for rnd, ct, t, side, winner in m["rounds"]:
            if rnd <= 24:
                per_map[m["map"]][0] += 1
                per_map[m["map"]][1] += side == "ct"
    for name, (n, ct_w) in sorted(per_map.items(), key=lambda kv: -kv[1][1] / kv[1][0]):
        if n >= 200:
            print(f"   {str(name):10} CT {ct_w / n:5.1%}  ({n} rounds, {n // 22} maps approx)")

    print("\n2. Team leading after 12 rounds: P(win map) by lead and first-half side")
    lead_stats = defaultdict(lambda: [0, 0])
    for m in maps:
        a, b = m["first_ct"], m["first_t"]
        da = m["half"].get(a, 0) - m["half"].get(b, 0)
        if da == 0:
            continue
        leader, side = (a, "CT") if da > 0 else (b, "T")
        lead = min(abs(da), 8)
        s = lead_stats[(lead, side)]
        s[0] += 1
        s[1] += m["winner"] == leader
    print(f"   {'lead':>5} {'leader was CT':>20} {'leader was T':>20}")
    for lead in range(1, 9):
        cells = []
        for side in ("CT", "T"):
            n, w = lead_stats[(lead, side)]
            cells.append(f"{w / n:6.1%} (n={n:4d})" if n else " " * 15)
        print(f"   {'+' + str(lead) + ('+' if lead == 8 else ''):>5} {cells[0]:>20} {cells[1]:>20}")

    print(f"\n3. Favorite (PM match price >= {args.fav:.2f}) trails after 12 rounds, underdog played CT")
    scen = defaultdict(lambda: [0, 0])
    base = [0, 0]
    joined = 0
    for m in maps:
        fav = pm_favorite(conn, m["external_id"], args.fav)
        if not fav:
            continue
        joined += 1
        fav_team = fav[0]
        if fav_team not in (m["first_ct"], m["first_t"]):
            continue
        base[0] += 1
        base[1] += m["winner"] == fav_team
        dog = m["first_t"] if fav_team == m["first_ct"] else m["first_ct"]
        deficit = m["half"].get(dog, 0) - m["half"].get(fav_team, 0)
        if deficit > 0:
            dog_side = "CT" if dog == m["first_ct"] else "T"
            won = m["winner"] == fav_team
            for key in (f"dog {dog_side} / ALL", f"dog {dog_side} / deficit {min(deficit, 8)}{'+' if deficit >= 8 else ''}",
                        f"dog {dog_side} / map {m['map'] or '?'}"):
                scen[key][0] += 1
                scen[key][1] += won
    print(f"   maps joined to Polymarket with a clear favorite: {joined}; favorite map win rate overall: "
          f"{base[1] / max(base[0], 1):.1%} (n={base[0]})")
    print("   favorite trails after 12 rounds; 'dog CT' = underdog played CT in the first half (favorite gets CT next)")
    for key, (n, w) in sorted(scen.items(), key=lambda kv: (kv[0].split(" / ")[1].startswith("map"), kv[0])):
        if n >= 10:
            se2 = 2 * (w / n * (1 - w / n) / n) ** 0.5
            print(f"   {key:28} favorite won {w / n:6.1%} ±{se2:5.1%}  (n={n})")


if __name__ == "__main__":
    main()
