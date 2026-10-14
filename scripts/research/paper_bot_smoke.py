"""Smoke test of the paper bot without network: temp live DBs, Telegram off, fake games, maker fills (CS2) and
map 2 rules (Dota 2).

  python scripts/research/paper_bot_smoke.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "PAPER_RULES", "PAPER_SPORT"):
    os.environ.pop(k, None)

from pmbot.live import paper, report, store  # noqa: E402

tmp = Path(tempfile.mkdtemp())
_connect = store.connect
store.connect = lambda path=store.LIVE_DB: _connect(tmp / path.name)

import paper_bot  # noqa: E402


def cs2() -> None:
    bot = paper_bot.Bot()
    print("rules:", bot.all_rules)
    game = {"game_id": 1, "event_id": "e1", "title": "Counter-Strike: A vs B (BO3) - IEM Test", "tournament": "IEM Test",
            "best_of": 3, "markets": [
                {"market_id": "m0", "condition_id": "c0", "market_type": "moneyline", "map_number": None,
                 "outcome_a": "A", "outcome_b": "B", "token_a": "ta", "token_b": "tb"},
                {"market_id": "m1", "condition_id": "c1", "market_type": "child_moneyline", "map_number": 1,
                 "outcome_a": "A", "outcome_b": "B", "token_a": "t1a", "token_b": "t1b"}]}
    bot.games[1] = game
    item = {"gameId": 1, "homeTeam": "A", "awayTeam": "B", "score": "5-3|0-0|Bo3", "period": "1/3", "live": True}
    bot.live[1] = item
    print("tier1:", paper.is_tier1(game), "| live markets:", [(g, m, mk["market_id"]) for g, _, _, m, mk in bot.live_markets()])
    market = game["markets"][1]
    bot.maker_fill("maker_t1", 1, game, item, 1, market, True, 0.78, 8.0)
    bot.maker_fill("maker_t1", 1, game, item, 1, market, True, 0.80, 50.0)
    bot.maker_fill("maker_t1", 1, game, item, 1, market, False, 0.75, 50.0)  # other side: ignored
    print(bot.conn.execute("SELECT strategy, outcome, stake, shares, avg_price, filled, status, state FROM paper_trades")
          .fetchall())
    print(report.summary(bot.conn))


async def dota2() -> None:
    os.environ["PAPER_SPORT"], os.environ["PAPER_STAKE_PCT"] = "dota2", "1"
    bot = paper_bot.Bot()
    print("\ndota2 rules:", bot.all_rules, "| db:", store.live_db(bot.sport).name)

    async def fake_book(token: str) -> dict:
        return {"asks": [(0.47, 5.0), (0.49, 100.0)], "ask": 0.47}
    bot.fetch_book = fake_book

    def game(gid: int, tournament: str, best_of: int = 3) -> dict:
        return {"game_id": gid, "event_id": f"e{gid}", "title": f"Dota 2: A vs B (BO{best_of}) - {tournament}",
                "tournament": tournament, "best_of": best_of, "markets": [
                    {"market_id": f"m{gid}", "market_type": "moneyline", "map_number": None,
                     "outcome_a": "A", "outcome_b": "B", "token_a": f"t{gid}a", "token_b": f"t{gid}b"},
                    {"market_id": f"m{gid}-2", "market_type": "child_moneyline", "map_number": 2,
                     "outcome_a": "A", "outcome_b": "B", "token_a": f"t{gid}-2a", "token_b": f"t{gid}-2b"}]}

    cases = [  # (game, prematch price of A, frames)
        (game(11, "DreamLeague Season 28"), 0.45, ["1/3 12-9|0-0", "2/3 000-000|0-1"]),       # A lost, 2.0-2.5
        (game(12, "ESL One Test"), 0.35, ["1/3 3-3|0-0", "2/3 000-000|0-0", "2/3 000-000|0-1"]),  # 2.5-3.33, late maps
        (game(13, "BLAST Slam VI"), 0.60, ["1/3 1-0|0-0", "2/3 000-000|0-1"]),                # favourite lost
        (game(14, "Some Cup"), 0.45, ["1/3 1-0|0-0", "2/3 000-000|0-1"]),                     # tier2: nothing
        (game(15, "DreamLeague Season 28"), 0.45, ["2/3 5-5|0-1"]),                           # map 1 not seen
        (game(16, "DreamLeague Season 28"), 0.45, ["1/3 1-0|0-0", "2/3 000-000|1-0"]),        # A won map 1
    ]
    for g, p, frames in cases:
        bot.games[g["game_id"]] = g
        bot.prematch[g["game_id"]] = {"A": p, "B": 1 - p}
        for f in frames:
            period, score = f.split()
            await bot.evaluate_maps(g["game_id"], {"homeTeam": "A", "awayTeam": "B", "score": f"{score}|Bo3",
                                                   "period": period})
    for row in bot.conn.execute("SELECT strategy, game_id, map_number, outcome, stake, avg_price, filled, status "
                                "FROM paper_trades ORDER BY game_id, strategy"):
        print(row)
    print(report.summary(bot.conn))


if __name__ == "__main__":
    cs2()
    asyncio.run(dota2())
