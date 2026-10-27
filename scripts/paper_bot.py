"""Live recorder + paper trading bot of one sport: CS2, Dota 2, or tennis (no real orders, no private key needed).

- discovery: open events of the sport from Gamma every 60 s (markets, tokens);
- score: Polymarket sports WebSocket, every change stored with arrival time; CS2 games it sends without rounds
  ('000-000': PGL, NODWIN, ESEA...) get the round score from Olimpbet live (status / score_src 'olimpbet');
- order book of the current map / match market every BOOK_INTERVAL s, stored on change;
- paper trades by pmbot/live/paper.py rules, filled on the real book after 1 s (Polymarket delay);
  CS2 - on the round score of the current map; Dota 2 - on the map score when the feed switches to the next map;
  tennis - after set 1 (period S2+), buy set-1 winner on moneyline (R-23b);
- paper maker (CS2 tier1): taker tape of the current map market polled every MAKER_INTERVAL s, bid fills simulated;
- settlement from Gamma, Telegram notifications, daily summary;
- CS2: daily cybersport.ru collection (first-half sides / rounds for later analysis).

Stake = PAPER_STAKE_PCT % of the rule's own virtual bank (PAPER_BANK_USD at the first start + settled P&L).

Env (all optional): PAPER_SPORT=cs2 (or dota2 / tennis; database data/live.sqlite / data/live_<sport>.sqlite),
PAPER_BANK_USD=1000, PAPER_STAKE_PCT=2, PAPER_RULES (comma list, default paper.DEFAULT_RULES[sport]),
BOOK_INTERVAL=10, MAKER_INTERVAL=10, OLIMPBET_INTERVAL=5 (0 = off), REPORT_HOUR_UTC=18, CS_COLLECT_HOUR_UTC=3,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.

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

from pmbot import olimpbet, polymarket  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402
from pmbot.join import norm_team  # noqa: E402
from pmbot.live import feeds, paper, report, store  # noqa: E402
from pmbot.telegram import Telegram  # noqa: E402

log = logging.getLogger("paper_bot")
NO_ROUNDS_WARN_S = 300
SPORT_LABEL = {"cs2": "CS2", "dota2": "Dota 2", "tennis": "Теннис"}
RULE_LABEL = {
    "leader_half": "CS2: лидер на перерыве +4",
    "leader_half6": "CS2: лидер на перерыве +6",
    "leader_late": "CS2: лидер во 2-й половине",
    "fav_band_t1": "CS2: фаворит кэф 1.09–1.43 на перерыве (tier1)",
    "maker_t1": "CS2: мейкер на фаворита кэф 1.09–1.43 (tier1)",
    "dota_dog_m2": "Dota: аутсайдер 2.0–2.5 после карты 1",
    "dota_dog3_m2": "Dota: аутсайдер 2.5–3.33 после карты 1",
    "dota_loser_m2": "Dota: проигравший карту 1 на карту 2",
    "tennis_s1_winner": "Теннис: победитель сета 1",
    "tennis_dog_s1": "Теннис: аутсайдер после своего S1",
    "tennis_fav_s1": "Теннис: фаворит после своего S1",
}


def _odds(price: float | None) -> str:
    """Decimal odds from Polymarket price (0–1)."""
    if not price or price <= 0:
        return "—"
    return f"{1 / price:.2f}"


def _rule_line(rule: str) -> str:
    label = RULE_LABEL.get(rule)
    return f"{label} ({rule})" if label else rule


class Bot:
    def __init__(self) -> None:
        self.sport = os.getenv("PAPER_SPORT") or "cs2"
        if self.sport not in paper.SPORT_RULES:
            raise SystemExit(f"PAPER_SPORT must be one of {sorted(paper.SPORT_RULES)}")
        self.bank_usd = float(os.getenv("PAPER_BANK_USD", "1000"))
        self.stake_pct = float(os.getenv("PAPER_STAKE_PCT", "2"))
        allowed = paper.SPORT_RULES[self.sport]
        default = ",".join(paper.DEFAULT_RULES.get(self.sport, ()))
        names = [n.strip() for n in (os.getenv("PAPER_RULES") or default).split(",") if n.strip()]
        names = [n for n in names if n in allowed]
        score_rules = paper.RULES | paper.DOTA_RULES | paper.TENNIS_RULES
        self.rules = {n: score_rules[n] for n in names if n in score_rules}
        self.band_rules = {n: paper.BAND_RULES[n] for n in names if n in paper.BAND_RULES}
        self.maker_rules = {n: paper.MAKER_RULES[n] for n in names if n in paper.MAKER_RULES}
        self.book_interval = float(os.getenv("BOOK_INTERVAL", "10"))
        self.maker_interval = float(os.getenv("MAKER_INTERVAL", "10"))
        self.report_hour = int(os.getenv("REPORT_HOUR_UTC", "18"))
        self.cs_hour = int(os.getenv("CS_COLLECT_HOUR_UTC", "3"))
        self.conn = store.connect(store.live_db(self.sport))
        for name in self.all_rules:
            store.open_bank(self.conn, name, self.bank_usd)
        self.tg = Telegram()
        self.games: dict[int, dict] = {}        # game_id -> parsed event
        self.live: dict[int, dict] = {}         # game_id -> last WS item
        self.last_key: dict[int, tuple] = {}
        self.prematch: dict[int, dict[str, float]] = {}
        self.seen_maps: dict[int, set[int]] = {}  # game_id -> map/set numbers seen live in this run
        self.last_book: dict[str, tuple] = {}
        self.fired = {(s, g, m) for s, g, m in self.conn.execute(
            "SELECT strategy, game_id, map_number FROM paper_trades")}
        self.trade_lock = asyncio.Lock()
        self.http_trade = HttpClient(min_interval=0.1, retries=3, timeout=15)
        # CS2 round score from Olimpbet for games the WS sends without rounds ('000-000': PGL, NODWIN, ESEA...)
        self.ob_interval = float(os.getenv("OLIMPBET_INTERVAL", "5"))
        self.score_src: dict[int, str] = {}       # game_id -> fallback score source of its trades
        self.ob_last: dict[int, tuple] = {}
        self.no_rounds: dict[int, float] = {}     # game_id -> first time live without rounds in any source
        self.no_rounds_warned: set[int] = set()

    @property
    def all_rules(self) -> list[str]:
        return [*self.rules, *self.band_rules, *self.maker_rules]

    def live_markets(self) -> list[tuple[int, dict, dict, int, dict]]:
        """(game id, game, last WS item, map/set number, market) of every live game."""
        out = []
        for gid, item in list(self.live.items()):
            game = self.games.get(gid)
            if not game or not item.get("live") or item.get("ended"):
                continue
            if self.sport == "tennis":
                set_no = paper.tennis_period_set(item.get("period")) or 1
                market = paper.moneyline_market(game)
                if market:
                    out.append((gid, game, item, set_no, market))
                continue
            parsed = paper.parse_score(item.get("score"))
            try:
                map_number = int(str(item.get("period") or "").split("/")[0])
            except ValueError:
                continue
            market = paper.map_market(game, map_number, parsed[1] if parsed else (0, 0)) if map_number >= 1 else None
            if market:
                out.append((gid, game, item, map_number, market))
        return out

    # ---------- discovery ----------
    async def discovery_loop(self) -> None:
        http = HttpClient(min_interval=0.3)
        while True:
            try:
                events = []
                for series_id in polymarket.SPORT_SERIES[self.sport]:
                    events += await asyncio.to_thread(feeds.open_events, http, 2, series_id)
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
        async for item in feeds.sports_updates(feeds.LEAGUES[self.sport]):
            gid = item.get("gameId")
            if gid is None:
                continue
            prev = self.last_key.get(gid)
            if (prev and str(item.get("score") or "").startswith("000-000") and item.get("period") == prev[1]
                    and not item.get("ended")):
                # the feed interleaves '000-000' frames for all games every ~20 s; on a new map it is a real reset
                continue
            key = (item.get("score"), item.get("period"), item.get("status"), item.get("ended"))
            self.live[gid] = item
            if self.last_key.get(gid) == key or gid not in self.games:
                continue
            self.last_key[gid] = key
            store.save_score(self.conn, gid, item.get("score"), item.get("period"), item.get("status"),
                             item.get("homeTeam"), item.get("awayTeam"))
            if item.get("live") and not item.get("ended"):
                if self.sport == "dota2":
                    asyncio.create_task(self.evaluate_maps(gid, item))
                elif self.sport == "tennis":
                    asyncio.create_task(self.evaluate_tennis(gid, item))
                else:
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
        if paper.HALF_ROUNDS[0] <= state.total <= paper.HALF_ROUNDS[1] and paper.is_tier1(game, self.sport):
            for name, band in self.band_rules.items():
                if (name, gid, map_number) not in self.fired:
                    self.fired.add((name, gid, map_number))
                    await self.open_band_trade(name, gid, game, state, (m_home, m_away), band)

    async def evaluate_maps(self, gid: int, item: dict) -> None:
        """Dota 2 tier1 BO3: rules on the map score, only if the previous map of the game was seen live in this run
        (after a restart the feed may already be deep into the map, and '000-000' frames come at any time)."""
        game = self.games[gid]
        maps = paper.parse_maps(item.get("score"))
        teams = self.team_names(game, item)
        try:
            map_number = int(str(item.get("period") or "").split("/")[0])
        except ValueError:
            return
        seen = self.seen_maps.setdefault(gid, set())
        seen.add(map_number)
        if (not maps or not teams or map_number - 1 not in seen or game.get("best_of") != 3
                or map_number != sum(maps) + 1 or not paper.is_tier1(game, self.sport)):
            return
        if all((name, gid, map_number) in self.fired for name in self.rules):
            return
        prematch = await self.get_prematch(gid, game)
        if not prematch:
            return
        parsed = paper.parse_score(item.get("score"))
        kills = parsed[0] if parsed else (0, 0)
        state = paper.MapState(map_number, dict(zip(teams, kills)), dict(zip(teams, maps)), prematch)
        for name, rule in self.rules.items():
            team = rule(state)
            if team and (name, gid, map_number) not in self.fired:
                self.fired.add((name, gid, map_number))
                await self.open_trade(name, gid, game, state, team, maps)

    async def evaluate_tennis(self, gid: int, item: dict) -> None:
        """After set 1 (R-23b): buy set-1 winner on moneyline when the feed enters S2+.
        Requires set 1 seen live in this run (restart mid-S2 is skipped)."""
        game = self.games[gid]
        set_no = paper.tennis_period_set(item.get("period"))
        if not set_no:
            return
        seen = self.seen_maps.setdefault(gid, set())
        seen.add(set_no)
        if set_no < 2 or 1 not in seen:
            return
        sets = paper.parse_tennis_sets(item.get("score"))
        teams = self.team_names(game, item)
        if not sets or not teams or not paper.tennis_set_complete(*sets[0]):
            return
        map_number = 1  # one signal per match after set 1
        if all((name, gid, map_number) in self.fired for name in self.rules):
            return
        prematch = await self.get_prematch(gid, game)
        if not prematch:
            return
        home_won = sets[0][0] > sets[0][1]
        maps = {teams[0]: int(home_won), teams[1]: int(not home_won)}
        games = {teams[0]: sets[0][0], teams[1]: sets[0][1]}
        state = paper.MapState(map_number, games, maps, prematch)
        for name, rule in self.rules.items():
            team = rule(state)
            if team and (name, gid, map_number) not in self.fired:
                self.fired.add((name, gid, map_number))
                await self.open_tennis_trade(name, gid, game, state, team)

    async def fetch_book(self, token: str) -> dict | None:
        async with self.trade_lock:
            try:
                return await asyncio.to_thread(feeds.fetch_book, self.http_trade, token)
            except Exception:
                log.exception("book fetch failed")
                return None

    async def open_trade(self, rule: str, gid: int, game: dict, s: paper.MapState, team: str,
                         maps_won: tuple[int, int]) -> None:
        market = paper.map_market(game, s.map_number, maps_won)
        token = paper.token_for(market, team) if market else None
        if not token:
            log.info("%s: no market for %s map %s", rule, game["title"], s.map_number)
            return
        await asyncio.sleep(1)
        book = await self.fetch_book(token)
        if book:
            self.paper_buy(rule, gid, game, s, team, market, token, book)

    async def open_tennis_trade(self, rule: str, gid: int, game: dict, s: paper.MapState, team: str) -> None:
        market = paper.moneyline_market(game)
        token = paper.token_for(market, team) if market else None
        if not token:
            log.info("%s: no moneyline for %s", rule, game["title"])
            return
        await asyncio.sleep(1)
        book = await self.fetch_book(token)
        if book:
            self.paper_buy(rule, gid, game, s, team, market, token, book)

    async def open_band_trade(self, rule: str, gid: int, game: dict, s: paper.MapState, maps_won: tuple[int, int],
                              band: tuple[float, float]) -> None:
        """Buy the side whose ask is in the band (no trade if none or both are)."""
        market = paper.map_market(game, s.map_number, maps_won)
        if not market:
            return
        await asyncio.sleep(1)
        books = {side: await self.fetch_book(market[f"token_{side}"]) for side in ("a", "b")}
        picks = [side for side, b in books.items() if b and b["ask"] is not None and band[0] <= b["ask"] <= band[1]]
        if len(picks) != 1:
            log.info("%s: %s map %s, no side in %s", rule, game["title"], s.map_number, band)
            return
        side = picks[0]
        self.paper_buy(rule, gid, game, s, market[f"outcome_{side}"], market, market[f"token_{side}"], books[side])

    def stake_for(self, rule: str) -> tuple[float, float]:
        """(stake, bankroll): PAPER_STAKE_PCT % of the rule's bank."""
        bankroll = store.bank(self.conn, rule) or self.bank_usd
        return round(max(bankroll, 0) * self.stake_pct / 100, 2), bankroll

    def paper_buy(self, rule: str, gid: int, game: dict, s: paper.MapState, team: str, market: dict, token: str,
                  book: dict) -> None:
        state = {"rounds": s.rounds, "maps": s.maps, "prematch": s.prematch}
        src = self.score_src.get(gid)
        if src:
            state["score_src"] = src
        stake, bankroll = self.stake_for(rule)
        if stake < 1:
            log.info("%s: bank %.2f$ exhausted", rule, bankroll)
            return
        fill = paper.simulate_fill(book["asks"], stake)
        status = "open" if fill["shares"] > 0 else "unfilled"
        reject_why = None
        ask = book.get("ask")
        if status == "open" and ask is not None and ask < paper.MIN_ASK.get(rule, 0):
            status, reject_why = "rejected", "ask противоречит счёту"
        if status == "rejected":
            fill = {**fill, "shares": 0, "filled": 0, "fee": 0}
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO paper_trades (strategy, game_id, event_id, market_id, token_id, outcome, map_number,
                       signal_t, state, stake, shares, avg_price, best_ask, fee, filled, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rule, gid, game["event_id"], market["market_id"], token, team, s.map_number, time.time(),
                 json.dumps(state, ensure_ascii=False), stake, fill["shares"], fill["avg_price"], book["ask"],
                 fill["fee"], fill["filled"], status))
        if cur.rowcount == 0:  # already traded before a restart
            return
        price = fill["avg_price"] if status == "open" else book.get("ask")
        # 📝 = ставка; ✅/❌ только на расчёте (выигрыш/проигрыш)
        if status == "unfilled":
            mark, status_line = "🚫", "не исполнено (пустой стакан)"
        elif status == "rejected":
            mark, status_line = "⚠️", f"пропущено ({reject_why})"
        elif fill["filled"] < 0.999:
            mark, status_line = "📝", f"частично {fill['filled']:.0%}"
        else:
            mark, status_line = "📝", "исполнено"
        context = self._context(s) + (" (счёт Olimpbet)" if src == "olimpbet" else "")
        self.tg.send(self._signal_msg(mark, rule, game["title"], context, team, stake, bankroll, price, status_line))

    def _context(self, s: paper.MapState) -> str:
        if self.sport == "dota2":
            score = " : ".join(f"{t} {n}" for t, n in s.maps.items())
            return f"карта {s.map_number}, по картам {score}"
        if self.sport == "tennis":
            score = " : ".join(f"{t} {n}" for t, n in s.rounds.items())
            return f"после сета 1 ({score})"
        score = " : ".join(f"{t} {n}" for t, n in s.rounds.items())
        return f"карта {s.map_number}, счёт {score}"

    def _signal_msg(self, mark: str, rule: str, title: str, context: str, outcome: str, stake: float,
                    bankroll: float, price: float | None, status_line: str) -> str:
        odds = f"{_odds(price)} (цена {price:.3f})" if price else "—"
        return (f"{mark} Стратегия: {_rule_line(rule)}\n"
                f"Событие: {title}\n"
                f"Контекст: {context}\n"
                f"Исход: {outcome}\n"
                f"Ставка: {stake:.2f}$ ({self.stake_pct:g}% банка)\n"
                f"Банк: {bankroll:.2f}$\n"
                f"Кэф: {odds}\n"
                f"Статус: {status_line}")

    def _settle_msg(self, won: bool, rule: str, title: str, context: str, outcome: str,
                    stake: float, pnl: float, bankroll: float | None, price: float | None) -> str:
        mark = "✅" if won else "❌"
        result = "зашла" if won else "не зашла"
        bank = f"{bankroll:.2f}$" if bankroll is not None else "—"
        odds = f"{_odds(price)} (цена {price:.3f})" if price else "—"
        return (f"{mark} Стратегия: {_rule_line(rule)}\n"
                f"Событие: {title}\n"
                f"Контекст: {context}\n"
                f"Исход: {outcome} — {result}\n"
                f"Ставка: {stake:.2f}$\n"
                f"Кэф: {odds}\n"
                f"P&L: {pnl:+.2f}$\n"
                f"Банк: {bank}")

    # ---------- CS2 round score fallback ----------
    async def olimpbet_loop(self) -> None:
        """Games live in the WS without rounds get the round score from Olimpbet and run the same rules; tier1
        games without rounds in both sources for NO_ROUNDS_WARN_S are reported once to Telegram."""
        if self.sport != "cs2" or self.ob_interval <= 0:
            return
        http = olimpbet.client(min_interval=1)
        down_since, warned = None, False
        while True:
            try:
                events = await asyncio.to_thread(olimpbet.fetch_live, http)
            except Exception as e:
                log.warning("olimpbet live: %r", e)
                down_since = down_since or time.time()
                if not warned and time.time() - down_since > NO_ROUNDS_WARN_S:
                    warned = True
                    self.tg.send(f"⚠️ Olimpbet не отвечает ({e!r:.150}): матчи без раундов в WS не торгуются")
            else:
                if warned:
                    self.tg.send("✅ Olimpbet снова отвечает")
                down_since, warned = None, False
                self.apply_olimpbet(events)
            await asyncio.sleep(self.ob_interval)

    def apply_olimpbet(self, events: list[dict]) -> None:
        now = time.time()
        for gid, item in list(self.live.items()):
            game = self.games.get(gid)
            if not game or not item.get("live") or item.get("ended") or paper.parse_score(item.get("score")):
                self.no_rounds.pop(gid, None)
                continue
            period = str(item.get("period") or "")
            ws_maps = paper.parse_maps(item.get("score"))
            try:
                map_number = int(period.split("/")[0])
            except ValueError:
                continue
            if map_number < 1 or not ws_maps:
                continue
            ev = paper.olimpbet_match(events, item.get("homeTeam"), item.get("awayTeam"))
            if not ev:
                first = self.no_rounds.setdefault(gid, now)
                if (now - first > NO_ROUNDS_WARN_S and gid not in self.no_rounds_warned
                        and paper.is_tier1(game, self.sport)):
                    self.no_rounds_warned.add(gid)
                    self.tg.send(f"⚠️ Нет счёта раундов (WS и Olimpbet), правила не работают\n"
                                 f"Событие: {game['title']}\nКарта: {period}")
                    log.info("no rounds in WS / olimpbet: %s", game["title"])
                continue
            self.no_rounds.pop(gid, None)
            rounds, maps, ob_map = ev["rounds"], ev["maps"], ev["map_number"]
            if ob_map != map_number:  # sources on different maps (switch in progress)
                continue
            key = (rounds, maps, ob_map)
            if self.ob_last.get(gid) == key:
                continue
            self.ob_last[gid] = key
            best_of = (item.get("score") or "").split("|")[-1]
            score = f"{rounds[0]}-{rounds[1]}|{maps[0]}-{maps[1]}|{best_of}"
            store.save_score_src(self.conn, gid, score, period, "olimpbet")
            self.score_src[gid] = "olimpbet"
            asyncio.create_task(self.evaluate(gid, {**item, "score": score}))

    # ---------- order books ----------
    async def books_loop(self) -> None:
        http = HttpClient(min_interval=0.1, retries=3, timeout=15)
        while True:
            for _, _, _, _, market in self.live_markets():
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

    # ---------- paper maker ----------
    async def maker_loop(self) -> None:
        """Tier1 live map markets: poll the taker tape, fill the paper bid on the favourite (paper.maker_fills)."""
        if not self.maker_rules:
            return
        http = HttpClient(min_interval=0.2, retries=2, timeout=15)
        quotes: dict[tuple[str, str], paper.MakerQuote] = {}
        while True:
            for gid, game, item, map_number, market in self.live_markets():
                if not paper.is_tier1(game, self.sport) or not market.get("condition_id"):
                    continue
                try:
                    trades = await asyncio.to_thread(polymarket.fetch_trades, http, market["condition_id"], 1)
                except Exception as e:
                    log.warning("trades %s: %r", market["market_id"], e)
                    continue
                for rule, band in self.maker_rules.items():
                    q = quotes.setdefault((rule, market["market_id"]), paper.MakerQuote())
                    for fav_a, bid, usd in paper.maker_fills(q, trades or [], market["token_a"], *band):
                        self.maker_fill(rule, gid, game, item, map_number, market, fav_a, bid, usd)
            await asyncio.sleep(self.maker_interval)

    def maker_fill(self, rule: str, gid: int, game: dict, item: dict, map_number: int, market: dict, fav_a: bool,
                   bid: float, usd: float) -> None:
        """One position per map: the first fill opens it (stake = % of bank), later fills on the same side top it up
        to the stake."""
        side = "a" if fav_a else "b"
        token, team = market[f"token_{side}"], market[f"outcome_{side}"]
        row = self.conn.execute(
            "SELECT id, token_id, stake, shares, filled, status FROM paper_trades "
            "WHERE strategy=? AND game_id=? AND map_number=?", (rule, gid, map_number)).fetchone()
        if row is None:
            stake, bankroll = self.stake_for(rule)
            if stake < 1:
                return
            take = min(usd, stake)
            parsed = paper.parse_score(item.get("score"))
            teams = self.team_names(game, item)
            state = {"bid": bid}
            if parsed and teams:
                state |= {"rounds": dict(zip(teams, parsed[0])), "maps": dict(zip(teams, parsed[1]))}
            with self.conn:
                self.conn.execute(
                    """INSERT OR IGNORE INTO paper_trades (strategy, game_id, event_id, market_id, token_id, outcome,
                           map_number, signal_t, state, stake, shares, avg_price, best_ask, fee, filled, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rule, gid, game["event_id"], market["market_id"], token, team, map_number, time.time(),
                     json.dumps(state, ensure_ascii=False), stake, take / bid, bid, None, 0.0, take / stake, "open"))
            score = " : ".join(f"{t} {n}" for t, n in state.get("rounds", {}).items()) or "?"
            self.tg.send(self._signal_msg(
                "📝", rule, game["title"], f"карта {map_number}, счёт {score} (мейкер)", team, stake, bankroll, bid,
                f"исполнено {take:.2f}$ из {stake:.2f}$"))
            return
        tid, row_token, stake, shares, filled, status = row
        spent = stake * (filled or 0)
        take = min(usd, stake - spent)
        if status != "open" or row_token != token or take < 0.01:
            return
        shares, spent = (shares or 0) + take / bid, spent + take
        with self.conn:
            self.conn.execute("UPDATE paper_trades SET shares=?, avg_price=?, filled=? WHERE id=?",
                              (shares, spent / shares, spent / stake, tid))

    # ---------- settlement ----------
    async def settle_loop(self) -> None:
        http = HttpClient(min_interval=0.3)
        while True:
            open_trades = self.conn.execute(
                "SELECT id, strategy, market_id, token_id, outcome, shares, stake, filled, fee, game_id, "
                "map_number, avg_price FROM paper_trades WHERE status='open'").fetchall()
            for tid, rule, market_id, token, outcome, shares, stake, filled, fee, gid, map_no, avg_price in open_trades:
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
                place = "матч" if self.sport == "tennis" else f"карта {map_no}"
                bankroll = store.bank(self.conn, rule)
                self.tg.send(self._settle_msg(bool(payout), rule, title, place, outcome,
                                              stake * (filled or 0), pnl, bankroll, avg_price))
            await asyncio.sleep(120)

    # ---------- daily jobs ----------
    async def daily_loop(self) -> None:
        done: set[tuple[str, dt.date]] = set()
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            if now.hour == self.report_hour and ("report", now.date()) not in done:
                done.add(("report", now.date()))
                self.tg.send(f"{SPORT_LABEL[self.sport]}. " + report.daily(self.conn))
            if self.sport == "cs2" and now.hour == self.cs_hour and ("cs", now.date()) not in done:
                done.add(("cs", now.date()))
                since = (now.date() - dt.timedelta(days=2)).isoformat()
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(ROOT / "scripts" / "collect_cybersport.py"), "--since", since,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await proc.wait()
                log.info("cybersport collection since %s: exit %s", since, proc.returncode)
            await asyncio.sleep(60)

    async def run(self) -> None:
        banks = {n: store.bank(self.conn, n) for n in self.all_rules}
        log.info("%s rules: %s, stake %g%% of bank, telegram %s", self.sport, banks, self.stake_pct,
                 "on" if self.tg.enabled else "off")
        banks_lines = "\n".join(f"• {_rule_line(n)}: {b:.2f}$" for n, b in banks.items())
        self.tg.send(f"🤖 Бот {SPORT_LABEL[self.sport]} запущен\n"
                     f"Ставка: {self.stake_pct:g}% от банка правила\n"
                     f"Банки:\n{banks_lines}")
        await asyncio.gather(self.discovery_loop(), self.ws_loop(), self.olimpbet_loop(), self.books_loop(),
                             self.maker_loop(), self.settle_loop(), self.daily_loop())


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(Bot().run())


if __name__ == "__main__":
    main()
