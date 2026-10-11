"""Score frames of live games matching a title substring, from a live.sqlite copy.

  python scripts/research/live_scores_dump.py "MOUZ vs Spirit" [data/server_live.sqlite] [--from HH:MM --to HH:MM]
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot.live import store  # noqa: E402


def main() -> None:
    title = sys.argv[1]
    path = Path(sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else "data/server_live.sqlite")
    conn = store.connect(path)
    for gid, full in conn.execute("SELECT game_id, title FROM live_games WHERE title LIKE ?", (f"%{title}%",)):
        print(f"== {gid} {full}")
        for t, score, period, status in conn.execute(
                "SELECT t, score, period, status FROM live_scores WHERE game_id = ? ORDER BY t", (gid,)):
            print(f"  {dt.datetime.fromtimestamp(t, dt.timezone.utc):%m-%d %H:%M:%S} {score:24} {period} {status}")


if __name__ == "__main__":
    main()
