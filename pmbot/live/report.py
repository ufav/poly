"""Paper trading summary per rule (used by scripts/paper_report.py and the daily Telegram message)."""

import sqlite3
import time

from pmbot.live import store


def summary(conn: sqlite3.Connection, since: float | None = None) -> str:
    # 'invalid' = trades on a broken feed score, marked by hand (docs/research.md R-14)
    where, args = ("WHERE status != 'invalid' AND signal_t >= ?", (since,)) if since else ("WHERE status != 'invalid'", ())
    rows = conn.execute(
        f"""SELECT strategy, COUNT(*), SUM(status='settled'), SUM(status='open'), SUM(payout > 0),
                   SUM(CASE WHEN status='settled' THEN stake * filled END), SUM(pnl),
                   AVG(CASE WHEN status IN ('open', 'settled') THEN avg_price END), AVG(filled),
                   SUM(status IN ('unfilled', 'rejected'))
            FROM paper_trades {where} GROUP BY strategy ORDER BY strategy""", args).fetchall()
    games = conn.execute("SELECT COUNT(*) FROM live_games WHERE first_seen >= ?", (since or 0,)).fetchone()[0]
    lines = [f"Матчей записано: {games}"]
    if not rows:
        lines.append("Сделок нет")
    for name, n, settled, open_, wins, staked, pnl, avg_price, filled, unfilled in rows:
        settled, open_, wins = settled or 0, open_ or 0, wins or 0
        roi = f"{pnl / staked:+.1%}" if staked else "—"
        b = store.bank(conn, name)
        bank = f"{b:.2f}$" if b is not None else "—"
        odds = f"{1 / avg_price:.2f}" if avg_price else "—"
        lines.append(
            f"• {name}\n"
            f"  банк {bank} | сигналов {n} | ✅ {wins} / ❌ {(settled or 0) - (wins or 0)} / открыто {open_} / "
            f"без исполнения {unfilled or 0}\n"
            f"  P&L {pnl or 0:+.2f}$ ROI {roi} | ср. кэф {odds} | ср. исполнение {(filled or 0):.0%}"
        )
    return "\n".join(lines)


def daily(conn: sqlite3.Connection) -> str:
    return ("Итог за 24 ч\n" + summary(conn, time.time() - 86400) + "\n\nВсего\n" + summary(conn))
