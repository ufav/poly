"""Live CS2 recorder + paper trading bot (no real orders, no private key needed).

- discovery: open CS2 events from Gamma every 60 s (markets, tokens);
- score: Polymarket sports WebSocket, every change stored with arrival time;
- order book of the current map market every BOOK_INTERVAL s, stored on change;
- paper trades by pmbot/live/paper.py rules, filled on the real book after 1 s (Polymarket delay);
- settlement from Gamma, Telegram notifications, daily summary;
- daily cybersport.ru collection (first-half sides / rounds for later analysis).

Env (all optional): PAPER_STAKE_USD=100, PAPER_RULES=fav_comeback_half,fav_comeback_wide,leader_half,leader_half6,leader_late,
BOOK_INTERVAL=10, REPORT_HOUR_UTC=18, CS_COLLECT_HOUR_UTC=3, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.

  python scripts/paper_bot.py
"""

import asyncio
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from pmbot.http import HttpClient  # noqa: E402
from pmbot.join import norm_team  # noqa: E402
from pmbot.live import feeds, paper, report, store  # noqa: E402
from pmbot.telegram import Telegram  # noqa: E402

log = logging.getLogger("paper_bot")


class Bot:
    def __init__(self) -> None:
        self.stake = float(os.getenv("PAPER_STAKE_USD", "100"))
        names = os.getenv("PAPER_RULES") or ",".join(paper.RULES)
        self.rules = {n.strip(): paper.RULES[n.strip()] for n in names.split(",") if n.strip() in paper.RULES}
        self.book_interval = float(os.getenv("BOOK_INTERVAL", "10"))
        self.report_hour = int(os.getenv("REPORT_HOUR_UTC", "18"))
        self.cs_hour = int(os.getenv("CS_COLLECT_HOUR_UTC", "3"))
        self.conn = store.connect()
        self.tg = Telegram()
        self.games: dict[int, dict] = {}        # game_id -> parsed event
        self.live: dict[int, dict] = {}         # game_id -> last WS item
        self.last_key: dict[int, tuple] = {}
        self.prematch: dict[int, dict[str, float]] = {}
        self.last_book: dict[str, tuple] = {}
        self.fired = {(s, g, m) for s, g, m in self.conn.execute(
            "SELECT strategy, game_id, map_number FROM paper_trades")}
        self.trade_lock = asyncio.Lock()
        self.http_trade = HttpClient(min_interval=0.1, retries=3, timeout=15)

    # ---------- discovery ----------
    async def discovery_loop(self) -> None:
        http = HttpClient(min_interval=0.3)
        while True:
            try:
                events = await asyncio.to_thread(feeds.open_events, http)
                for e in events:
                    g = feeds.parse_event(e)
                    if not g["game_id"] or not g["markets"]:
                        continue
                    if g["game_id"] not in self.games:
                        store.save_game(self.conn, g)
                        log.info("new game %s: %s", g["game_id"], g["title"])
                    self.games[g["game_id"]] = g
            except Exception:
                log.exception("discovery failed")
            await asyncio.sleep(60)

    # ---------- live score ----------
    async def ws_loop(self) -> None:
        async for item in feeds.sports_updates():
            gid = item.get("gameId")
            if gid is None:
                continue
            prev = self.last_key.get(gid)
            if (prev and str(item.get("score") or "").startswith("000-000") and item.get("period") == prev[1]
                    and not item.get("ended")):
                # the feed interleaves '000-000' frames for all CS2 games every ~20 s; on a new map it is a real reset
                continue
            key = (item.get("score"), item.get("period"), item.get("status"), item.get("ended"))
            self.live[gid] = item
            if self.last_key.get(gid) == key or gid not in self.games:
                continue
            self.last_key[gid] = key
            store.save_score(self.conn, gid, item.get("score"), item.get("period"), item.get("status"),
                             item.get("homeTeam"), item.get("awayTeam"))
            if item.get("live") and not item.get("ended"):
                asyncio.create_task(self.evaluate(gid, item))
            if item.get("ended"):
                with self.conn:
                    self.conn.execute("UPDATE live_games SET ended_at=COALESCE(ended_at, ?) WHERE game_id=?",
                                      (time.time(), gid))

    def team_names(self, game: dict, item: dict) -> tuple[str, str] | None:
        """WS home/away -> market outcome names (same order)."""
        ml = next((m for m in game["markets"] if m["market_type"] == "moneyline"), None)
        if not ml:
            return None
        outcomes = {norm_team(ml["outcome_a"]): ml["outcome_a"], norm_team(ml["outcome_b"]): ml["outcome_b"]}
        home, away = outcomes.get(norm_team(item.get("homeTeam"))), outcomes.get(norm_team(item.get("awayTeam")))
        return (home, away) if home and away and home != away else None

    async def get_prematch(self, gid: int, game: dict) -> dict[str, float] | None:
        if gid not in self.prematch:
            ml = next((m for m in game["markets"] if m["market_type"] == "moneyline"), None)
            if not ml:
                return None
            async with self.trade_lock:
                p = await asyncio.to_thread(feeds.prematch_price, self.http_trade, ml["token_a"], game["start_time"])
            if p is None:
                return None
            self.prematch[gid] = {ml["outcome_a"]: p, ml["outcome_b"]: 1 - p}
            with self.conn:
                self.conn.execute("UPDATE live_games SET prematch_home=? WHERE game_id=?", (p, gid))
        return self.prematch[gid]

    async def evaluate(self, gid: int, item: dict) -> None:
        game = self.games[gid]
        parsed = paper.parse_score(item.get("score"))
        period = str(item.get("period") or "")
        if not parsed or "/" not in period:
            return
        try:
            map_number = int(period.split("/")[0])
        except ValueError:
            return
        teams = self.team_names(game, item)
        if not teams or map_number < 1:
            return
        (r_home, r_away), (m_home, m_away) = parsed
        if r_home + r_away < paper.HALF_ROUNDS[0] or not paper.consistent(map_number, (r_home, r_away), (m_home, m_away)):
            return
        prematch = await self.get_prematch(gid, game)
        if not prematch:
            return
        state = paper.MapState(map_number, {teams[0]: r_home, teams[1]: r_away},
                               {teams[0]: m_home, teams[1]: m_away}, prematch)
        for name, rule in self.rules.items():
            team = rule(state)
            if team and (name, gid, map_number) not in self.fired:
                self.fired.add((name, gid, map_number))
                await self.open_trade(name, gid, game, state, team, (m_home, m_away))

    async def open_trade(self, rule: str, gid: int, game: dict, s: paper.MapState, team: str,
                         maps_won: tuple[int, int]) -> None:
        market = paper.map_market(game, s.map_number, maps_won)
        token = paper.token_for(market, team) if market else None
        state = {"rounds": s.rounds, "maps": s.maps, "prematch": s.prematch}
        if not token:
            log.info("%s: no market for %s map %s", rule, game["title"], s.map_number)
            return
        await asyncio.sleep(1)
        async with self.trade_lock:
            try:
                book = await asyncio.to_thread(feeds.fetch_book, self.http_trade, token)
            except Exception:
                log.exception("book fetch failed")
                return
        fill = paper.simulate_fill(book["asks"], self.stake)
        status = "open" if fill["shares"] > 0 else "unfilled"
        if status == "open" and book["ask"] < paper.MIN_ASK.get(rule, 0):
            status = "rejected"
            fill = {**fill, "shares": 0, "filled": 0, "fee": 0}
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO paper_trades (strategy, game_id, event_id, market_id, token_id, outcome, map_number,
                       signal_t, state, stake, shares, avg_price, best_ask, fee, filled, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rule, gid, game["event_id"], market["market_id"], token, team, s.map_number, time.time(),
                 json.dumps(state, ensure_ascii=False), self.stake, fill["shares"], fill["avg_price"], book["ask"],
                 fill["fee"], fill["filled"], status))
        if cur.rowcount == 0:  # already traded before a restart
            return
        score = " : ".join(f"{t} {n}" for t, n in s.rounds.items())
        fav = max(s.prematch, key=s.prematch.get)
        if status == "unfilled":
            result = f"Не исполнено: в стакане {team} нет заявок на продажу"
        elif status == "rejected":
            result = f"Пропущено: ask {book['ask']} противоречит счёту (вероятно, сбой фида)"
        else:
            result = (f"Покупка (бумажная): {team} по {fill['avg_price']:.3f} (лучший ask {book['ask']}), "
                      f"исполнено {fill['filled']:.0%} от {self.stake:.0f}$")
        self.tg.send(f"📝 {rule}\n{game['title']}\nКарта {s.map_number}, счёт: {score}\n"
                     f"Фаворит до матча: {fav} {s.prematch[fav]:.2f}\n{result}")

    # ---------- order books ----------
    async def books_loop(self) -> None:
        http = HttpClient(min_interval=0.1, retries=3, timeout=15)
        while True:
            for gid, item in list(self.live.items()):
                game = self.games.get(gid)
                if not game or not item.get("live") or item.get("ended"):
                    continue
                parsed = paper.parse_score(item.get("score"))
                period = str(item.get("period") or "")
                try:
                    map_number = int(period.split("/")[0])
                except ValueError:
                    continue
                maps_won = parsed[1] if parsed else (0, 0)
                market = paper.map_market(game, map_number, maps_won) if map_number >= 1 else None
                if not market:
                    continue
                for token in (market["token_a"], market["token_b"]):
                    try:
                        book = await asyncio.to_thread(feeds.fetch_book, http, token)
                    except Exception as e:
                        log.warning("book %s: %r", token[:10], e)
                        continue
                    key = (book["bid"], book["ask"], book["bid_size"], book["ask_size"])
                    if self.last_book.get(token) != key:
                        self.last_book[token] = key
                        store.save_book(self.conn, token, book)
            await asyncio.sleep(self.book_interval)

    # ---------- settlement ----------
    async def settle_loop(self) -> None:
        http = HttpClient(min_interval=0.3)
        while True:
            open_trades = self.conn.execute(
                "SELECT id, strategy, market_id, token_id, outcome, shares, stake, filled, fee, game_id, map_number "
                "FROM paper_trades WHERE status='open'").fetchall()
            for tid, rule, market_id, token, outcome, shares, stake, filled, fee, gid, map_no in open_trades:
                try:
                    m = await asyncio.to_thread(feeds.fetch_market, http, market_id)
                except Exception as e:
                    log.warning("market %s: %r", market_id, e)
                    continue
                payout = paper.settle(m, token)
                if payout is None:
                    continue
                pnl = (shares or 0) * payout - stake * (filled or 0) - (fee or 0)
                with self.conn:
                    self.conn.execute("UPDATE paper_trades SET status='settled', payout=?, pnl=?, settled_at=? WHERE id=?",
                                      (payout, pnl, time.time(), tid))
                title = self.games.get(gid, {}).get("title", gid)
                self.tg.send(f"{'✅' if payout else '❌'} {rule}\n{title}, карта {map_no}: {outcome} "
                             f"{'выиграл' if payout else 'проиграл'}\nP&L {pnl:+.2f}$")
            await asyncio.sleep(120)

    # ---------- daily jobs ----------
    async def daily_loop(self) -> None:
        done: set[tuple[str, dt.date]] = set()
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            if now.hour == self.report_hour and ("report", now.date()) not in done:
                done.add(("report", now.date()))
                self.tg.send(report.daily(self.conn))
            if now.hour == self.cs_hour and ("cs", now.date()) not in done:
                done.add(("cs", now.date()))
                since = (now.date() - dt.timedelta(days=2)).isoformat()
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(ROOT / "scripts" / "collect_cybersport.py"), "--since", since,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await proc.wait()
                log.info("cybersport collection since %s: exit %s", since, proc.returncode)
            await asyncio.sleep(60)

    async def run(self) -> None:
        log.info("rules: %s, stake %.0f$, telegram %s", list(self.rules), self.stake,
                 "on" if self.tg.enabled else "off")
        self.tg.send(f"🤖 Бумажный бот запущен. Правила: {', '.join(self.rules)}; ставка {self.stake:.0f}$")
        await asyncio.gather(self.discovery_loop(), self.ws_loop(), self.books_loop(), self.settle_loop(),
                             self.daily_loop())


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(Bot().run())


if __name__ == "__main__":
    main()
