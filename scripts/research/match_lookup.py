"""Find Polymarket events / markets and cybersport maps for a pair of teams.

  python scripts/research/match_lookup.py aurora vitality
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot import storage  # noqa: E402

a, b = (s.lower() for s in sys.argv[1:3])
conn = storage.connect()
for eid, title, start, score, vol in conn.execute(
        """SELECT id, title, start_time, score, volume FROM pm_events
           WHERE lower(title) LIKE ? AND lower(title) LIKE ? ORDER BY start_time DESC LIMIT 5""",
        (f"%{a}%", f"%{b}%")):
    print(f"PM event {eid}: {title} | start {start} | score {score} | vol {vol:.0f}")
    for mid, mtype, group, num, oa, ob, final, hist in conn.execute(
            """SELECT id, market_type, group_title, map_number, outcome_a, outcome_b, final_price_a, history_status
               FROM pm_markets WHERE event_id=? AND market_type IN ('moneyline','child_moneyline')""", (eid,)):
        print(f"   market {mid} {mtype} map={num} [{oa} vs {ob}] final_a={final} history={hist}")
for row in conn.execute(
        """SELECT id, date, time, team1, team2, score1, score2, tournament, begin_at, odds FROM cs_matches
           WHERE lower(team1||team2) LIKE ? AND lower(team1||team2) LIKE ? ORDER BY date DESC LIMIT 5""",
        (f"%{a}%", f"%{b}%")):
    print("CS match", row[:9])
    print("   odds", row[9][:300] if row[9] else None)
    for g in conn.execute("SELECT number, map_name, score1, score2, rounds FROM cs_games WHERE match_id=? ORDER BY number",
                          (row[0],)):
        print("   map", g)
