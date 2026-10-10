"""CT/T side advantage by map and how often the first-half leader wins the map, split by the side
the leader played in the first half (cybersport.ru rounds).

  python analysis/sides.py
"""

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402

conn = storage.connect()
games = conn.execute(
    """SELECT g.id, g.map_name FROM cs_games g WHERE g.rounds >= 13 AND g.map_name IS NOT NULL"""
).fetchall()

per_map = defaultdict(lambda: {"maps": 0, "rounds": 0, "ct_rounds": 0})
# key: (map or 'ALL', leader side in 1st half, lead size bucket) -> [n, leader won map]
half = defaultdict(lambda: [0, 0])

for game_id, map_name in games:
    rounds = conn.execute(
        "SELECT round, ct_team, t_team, winner_side, winner_team FROM cs_rounds WHERE game_id = ? ORDER BY round",
        (game_id,),
    ).fetchall()
    if len(rounds) < 13 or rounds[0][0] != 1:
        continue
    pm = per_map[map_name]
    pm["maps"] += 1
    for r, ct, t, side, winner in rounds:
        if r <= 24:
            pm["rounds"] += 1
            pm["ct_rounds"] += side == "ct"

    first_half = [x for x in rounds if x[0] <= 12]
    if len(first_half) < 12:
        continue
    ct_team, t_team = first_half[0][1], first_half[0][2]
    ct_won = sum(1 for x in first_half if x[4] == ct_team)
    t_won = 12 - ct_won
    if ct_won == t_won:
        continue
    leader, side, lead = (ct_team, "CT", ct_won - t_won) if ct_won > t_won else (t_team, "T", t_won - ct_won)
    totals = defaultdict(int)
    for x in rounds:
        totals[x[4]] += 1
    map_winner = max(totals, key=totals.get)
    bucket = "2" if lead <= 2 else "4" if lead <= 4 else "6+"
    for key in ((map_name, side, bucket), ("ALL", side, bucket), (map_name, side, "any"), ("ALL", side, "any")):
        half[key][0] += 1
        half[key][1] += map_winner == leader

print("CT side round win rate (regulation rounds 1-24)")
print(f"{'map':<10} {'maps':>5} {'CT rounds %':>12}")
for name, s in sorted(per_map.items(), key=lambda kv: -kv[1]["maps"]):
    if s["maps"] >= 10:
        print(f"{name:<10} {s['maps']:5d} {s['ct_rounds'] / s['rounds']:12.1%}")

print("\nFirst-half leader wins the map: by the side it led on and lead size (rounds)")
print(f"{'map':<10} {'side':>4} {'lead':>5} {'n':>5} {'leader wins map':>16}")
maps_sorted = ["ALL"] + [m for m, s in sorted(per_map.items(), key=lambda kv: -kv[1]["maps"]) if s["maps"] >= 10]
for name in maps_sorted:
    for side in ("CT", "T"):
        for bucket in ("any", "2", "4", "6+"):
            n, w = half.get((name, side, bucket), (0, 0))
            if n >= 8:
                print(f"{name:<10} {side:>4} {('+' + bucket) if bucket != 'any' else 'any':>5} {n:5d} {w / n:16.1%}")
    print()
