"""Why player stats don't map to teams: counts per failure reason.

  python scripts/research/player_team_check.py
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analysis"))

from pmbot import storage  # noqa: E402
from player_form import team_of  # noqa: E402


def main() -> None:
    conn = storage.connect()
    rosters = team_of(conn)
    reasons = Counter()
    examples = {}
    games = conn.execute(
        """SELECT DISTINCT s.game_id, m.date, m.team1_id, m.team2_id, m.team1, m.team2 FROM cs_player_stats s
           JOIN cs_games g ON g.id = s.game_id JOIN cs_matches m ON m.id = g.match_id""").fetchall()
    for gid, date, t1, t2, n1, n2 in games:
        if not t1 or not t2:
            reasons["no team ids in cs_matches"] += 1
            continue
        known = {str(t) for (t,) in conn.execute("SELECT id FROM cs_teams WHERE id IN (?,?)", (t1, t2))}
        if len(known) < 2:
            reasons["team roster not fetched"] += 1
            examples.setdefault("team roster not fetched", (n1, n2, t1, t2))
            continue
        c = Counter()
        for (pid,) in conn.execute("SELECT player_id FROM cs_player_stats WHERE game_id = ?", (gid,)):
            teams = {t for t, since, till in rosters.get(pid, []) if since <= date <= till}
            c["1" if str(t1) in teams else "2" if str(t2) in teams else "none"] += 1
        key = f"mapped {c['1']}/{c['2']} unknown {c['none']}"
        reasons[key] += 1
        examples.setdefault(key, (n1, n2, date))
    print(f"maps: {len(games)}")
    for k, v in reasons.most_common(15):
        print(f"  {v:5d}  {k}  e.g. {examples.get(k)}")


if __name__ == "__main__":
    main()
