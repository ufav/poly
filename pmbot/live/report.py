"""Paper trading summary per rule (used by scripts/paper_report.py and the daily Telegram message)."""

import sqlite3
import time


def summary(conn: sqlite3.Connection, since: float | None = None) -> str:
    where, args = ("WHERE signal_t >= ?", (since,)) if since else ("", ())
    rows = conn.execute(
        f"""SELECT strategy, COUNT(*), SUM(status='settled'), SUM(status='open'), SUM(payout > 0),
                   SUM(CASE WHEN status='settled' THEN stake * filled END), SUM(pnl),
                   AVG(CASE WHEN status != 'unfilled' THEN avg_price END), AVG(filled), SUM(status='unfilled')
            FROM paper_trades {where} GROUP BY strategy ORDER BY strategy""", args).fetchall()
    games = conn.execute("SELECT COUNT(*) FROM live_games WHERE first_seen >= ?", (since or 0,)).fetchone()[0]
    lines = [f"матчей записано: {games}"]
    if not rows:
        lines.append("сделок нет")
    for name, n, settled, open_, wins, staked, pnl, avg_price, filled, unfilled in rows:
        settled, open_, wins = settled or 0, open_ or 0, wins or 0
        roi = f"{pnl / staked:+.1%}" if staked else "—"
        lines.append(f"{name}: сигналов {n} (не исполнено {unfilled or 0}, открыто {open_}), рассчитано {settled}, "
                     f"выиграно {wins}, P&L {pnl or 0:+.2f}$ ROI {roi}, ср. цена {avg_price or 0:.3f}, "
                     f"ср. исполнение {(filled or 0):.0%}")
    return "\n".join(lines)


def daily(conn: sqlite3.Connection) -> str:
    return ("Итог за 24 ч\n" + summary(conn, time.time() - 86400) + "\n\nВсего\n" + summary(conn))
