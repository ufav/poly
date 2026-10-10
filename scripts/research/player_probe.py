"""Can match-stats players (csgo_player.internalId) be mapped to teams via cs_team_players.player_id?

  python scripts/research/player_probe.py [cybersport_match_id]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot import cybersport, storage  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402


def main(match_id: str) -> None:
    conn = storage.connect()
    print("cs_team_players rows:", conn.execute("SELECT COUNT(*), COUNT(DISTINCT player_id) FROM cs_team_players").fetchone())
    t1, t2 = conn.execute("SELECT team1_id, team2_id FROM cs_matches WHERE id = ?", (match_id,)).fetchone() or (None, None)
    print("teams:", t1, t2)
    st = cybersport.fetch_stats(HttpClient(), match_id)
    for x in st["included"]:
        if x["type"] != "csgo_player":
            continue
        a = x["attributes"]
        rows = conn.execute("SELECT team_id, nickname, since, till FROM cs_team_players WHERE player_id = ?",
                            (a["internalId"],)).fetchall()
        print(a["internalId"], a["externalName"][:30], "->", rows[:3])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "10099082")
