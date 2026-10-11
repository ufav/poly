"""Re-apply the feed sanity checks (paper.consistent, paper.MIN_ASK) to paper trades of a live.sqlite copy.

Prints the ids that would not have been opened and the summary without them (status set to 'invalid' in memory).

  python scripts/research/paper_recheck.py [data/server_live.sqlite]
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot.live import paper, report  # noqa: E402


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/server_live.sqlite")
    mem = sqlite3.connect(":memory:")
    sqlite3.connect(path).backup(mem)
    bad = []
    for tid, rule, m, state, ask, status in mem.execute(
            "SELECT id, strategy, map_number, state, best_ask, status FROM paper_trades"):
        s = json.loads(state)
        rounds, maps = tuple(s["rounds"].values()), tuple(s["maps"].values())
        if not paper.consistent(m, rounds, maps):
            bad.append((tid, rule, "stale/impossible score", status))
        elif status in ("open", "settled") and ask is not None and ask < paper.MIN_ASK.get(rule, 0):
            bad.append((tid, rule, f"ask {ask} below {paper.MIN_ASK[rule]}", status))
    for b in bad:
        print("invalid:", b)
    print("ids:", ",".join(str(b[0]) for b in bad), "\n")
    mem.executemany("UPDATE paper_trades SET status='invalid' WHERE id=?", [(b[0],) for b in bad])
    print(report.summary(mem))


if __name__ == "__main__":
    main()
