"""Paper trading report from data/live.sqlite.

  python scripts/paper_report.py [--trades 20]
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot.live import report, store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", type=int, default=20, help="show last N trades")
    args = ap.parse_args()
    conn = store.connect()
    print(report.daily(conn))
    print(f"\nпоследние {args.trades} сделок:")
    for row in conn.execute(
            """SELECT p.signal_t, p.strategy, g.title, p.map_number, p.outcome, p.state, p.avg_price, p.filled,
                      p.status, p.pnl FROM paper_trades p LEFT JOIN live_games g ON g.game_id = p.game_id
               ORDER BY p.signal_t DESC LIMIT ?""", (args.trades,)):
        t, strategy, title, map_no, outcome, state, price, filled, status, pnl = row
        when = dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%m-%d %H:%M")
        print(f"  {when} {strategy:18} {title} | карта {map_no} | {outcome} @ {price or 0:.3f} ({(filled or 0):.0%}) "
              f"| {state} | {status} {'' if pnl is None else f'{pnl:+.2f}$'}")


if __name__ == "__main__":
    main()
