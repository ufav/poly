"""Smoke test of the paper bot without network: temp live DB, Telegram off, fake game and maker fills.

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
for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "PAPER_RULES"):
    os.environ.pop(k, None)

from pmbot.live import paper, report, store  # noqa: E402

tmp = Path(tempfile.mkdtemp()) / "live.sqlite"
_connect = store.connect
store.connect = lambda path=tmp: _connect(path)

import paper_bot  # noqa: E402


def main() -> None:
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

    # grouped Telegram messages: two comeback rules on the same half, then their settlement
    sent = []
    bot.tg.send = sent.append
    bot.prematch[1] = {"A": 0.8, "B": 0.2}

    async def fake_book(token):
        return {"asks": [(0.35, 1000.0)], "ask": 0.35, "bids": [], "bid": None}

    bot.fetch_book = fake_book
    paper_bot.feeds.fetch_market = lambda http, mid: {
        "closed": True, "clobTokenIds": '["t1a", "t1b"]', "outcomePrices": '["0", "1"]'}
    half = {**item, "score": "4-8|0-0|Bo3"}

    async def run():
        await bot.evaluate(1, half)
        try:
            await asyncio.wait_for(bot.settle_loop(), timeout=3)
        except asyncio.TimeoutError:
            pass

    asyncio.run(run())
    print("\n---\n".join(sent))


if __name__ == "__main__":
    main()
