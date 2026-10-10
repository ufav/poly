import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot import storage  # noqa: E402

conn = storage.connect()
q = lambda sql, *a: conn.execute(sql, a).fetchall()

print("cs_matches:", q("SELECT stats_status, COUNT(*) FROM cs_matches GROUP BY 1"))
print("cs_games:", q("SELECT COUNT(*), SUM(rounds > 0) FROM cs_games")[0], "| cs_rounds:", q("SELECT COUNT(*) FROM cs_rounds")[0][0])

rows = q("""SELECT c.id, c.team1, c.team2, c.stats_team1, c.stats_team2, c.external_id, c.tournament,
                   e.title, e.team_a, e.team_b
            FROM cs_matches c LEFT JOIN pm_events e ON e.game_id = c.external_id
            WHERE c.stats_status = 'done'""")
print(f"\nwith rounds: {len(rows)}, joined to Polymarket by game_id: {sum(r[7] is not None for r in rows)}")
for r in rows:
    print(f"  {r[1]} vs {r[2]} | stats: {r[3]} vs {r[4]} | ext={r[5]} | PM: {r[8]} vs {r[9]}" if r[7] else f"  {r[1]} vs {r[2]} | ext={r[5]} | PM: not found")

print("\nmaps of one match:")
for g in q("""SELECT g.number, g.map_name, g.score1, g.score2, g.rounds,
                     (SELECT ct_team FROM cs_rounds WHERE game_id = g.id AND round = 1),
                     (SELECT COUNT(*) FROM cs_rounds WHERE game_id = g.id AND round <= 12 AND winner_team = (SELECT ct_team FROM cs_rounds WHERE game_id = g.id AND round = 1))
              FROM cs_games g WHERE g.match_id = '10099240' ORDER BY g.number"""):
    print(f"  map {g[0]} {g[1]}: {g[2]}:{g[3]} rounds={g[4]} | CT first half: {g[5]} won {g[6]}/12")
