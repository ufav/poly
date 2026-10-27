"""Live recorder / paper trading database (separate from the historical cs2.sqlite, easy to copy off the server)."""

import json
import sqlite3
import time
from pathlib import Path

LIVE_DB = Path(__file__).resolve().parents[2] / "data" / "live.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_games (
    game_id        INTEGER PRIMARY KEY,
    event_id       TEXT,
    slug           TEXT,
    title          TEXT,
    home           TEXT,
    away           TEXT,
    best_of        INTEGER,
    start_time     TEXT,
    prematch_home  REAL,
    first_seen     REAL,
    status         TEXT,
    ended_at       REAL
);

CREATE TABLE IF NOT EXISTS live_markets (
    market_id    TEXT PRIMARY KEY,
    event_id     TEXT,
    market_type  TEXT,
    map_number   INTEGER,
    outcome_a    TEXT,
    outcome_b    TEXT,
    token_a      TEXT,
    token_b      TEXT
);
CREATE INDEX IF NOT EXISTS live_markets_event ON live_markets(event_id);

-- every score/period change from the sports WebSocket, with arrival time
CREATE TABLE IF NOT EXISTS live_scores (
    game_id  INTEGER,
    t        REAL,
    score    TEXT,
    period   TEXT,
    status   TEXT,
    PRIMARY KEY (game_id, t)
) WITHOUT ROWID;

-- order book snapshots of the current map market, stored on change
CREATE TABLE IF NOT EXISTS live_books (
    token_id  TEXT,
    t         REAL,
    bid       REAL,
    ask       REAL,
    bid_size  REAL,
    ask_size  REAL,
    levels    TEXT,
    PRIMARY KEY (token_id, t)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS paper_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy    TEXT,
    game_id     INTEGER,
    event_id    TEXT,
    market_id   TEXT,
    token_id    TEXT,
    outcome     TEXT,
    map_number  INTEGER,
    signal_t    REAL,
    state       TEXT,
    stake       REAL,
    shares      REAL,
    avg_price   REAL,
    best_ask    REAL,
    fee         REAL,
    filled      REAL,
    status      TEXT,
    payout      REAL,
    pnl         REAL,
    settled_at  REAL,
    UNIQUE (strategy, game_id, map_number)
);

-- virtual bankroll per rule: start + P&L of trades settled since started_at (stake = % of it)
CREATE TABLE IF NOT EXISTS paper_bank (
    strategy    TEXT PRIMARY KEY,
    start_usd   REAL,
    started_at  REAL
);
"""


def live_db(sport: str = "cs2") -> Path:
    """data/live.sqlite for CS2 (as before), data/live_<sport>.sqlite for other sports."""
    return LIVE_DB if sport == "cs2" else LIVE_DB.with_name(f"live_{sport}.sqlite")


def connect(path: Path = LIVE_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def save_game(conn: sqlite3.Connection, g: dict) -> None:
    with conn:
        conn.execute(
            """INSERT INTO live_games (game_id, event_id, slug, title, home, away, best_of, start_time, first_seen, status)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(game_id) DO UPDATE SET event_id=excluded.event_id, slug=excluded.slug, title=excluded.title,
                   best_of=excluded.best_of, start_time=excluded.start_time""",
            (g["game_id"], g["event_id"], g["slug"], g["title"], g.get("home"), g.get("away"), g["best_of"],
             g["start_time"], time.time(), g.get("status")),
        )
        conn.executemany(
            """INSERT OR REPLACE INTO live_markets (market_id, event_id, market_type, map_number, outcome_a, outcome_b,
                                                   token_a, token_b) VALUES (?,?,?,?,?,?,?,?)""",
            [(m["market_id"], g["event_id"], m["market_type"], m["map_number"], m["outcome_a"], m["outcome_b"],
              m["token_a"], m["token_b"]) for m in g["markets"]],
        )


def save_score(conn: sqlite3.Connection, game_id: int, score: str | None, period: str | None, status: str | None,
               home: str | None, away: str | None) -> None:
    with conn:
        conn.execute("INSERT OR IGNORE INTO live_scores (game_id, t, score, period, status) VALUES (?,?,?,?,?)",
                     (game_id, time.time(), score, period, status))
        conn.execute("UPDATE live_games SET status=?, home=COALESCE(home, ?), away=COALESCE(away, ?) WHERE game_id=?",
                     (status, home, away, game_id))


def save_score_src(conn: sqlite3.Connection, game_id: int, score: str, period: str | None, src: str) -> None:
    """Score from a fallback source (status = source name), live_games untouched."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO live_scores (game_id, t, score, period, status) VALUES (?,?,?,?,?)",
                     (game_id, time.time(), score, period, src))


def open_bank(conn: sqlite3.Connection, strategy: str, start_usd: float) -> None:
    """Start the rule's bankroll now unless it already exists (delete the row to restart)."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO paper_bank (strategy, start_usd, started_at) VALUES (?,?,?)",
                     (strategy, start_usd, time.time()))


def bank(conn: sqlite3.Connection, strategy: str) -> float | None:
    row = conn.execute(
        """SELECT b.start_usd + COALESCE((SELECT SUM(p.pnl) FROM paper_trades p WHERE p.strategy = b.strategy
                  AND p.status = 'settled' AND p.signal_t >= b.started_at), 0)
           FROM paper_bank b WHERE b.strategy = ?""", (strategy,)).fetchone()
    return row[0] if row else None


def save_book(conn: sqlite3.Connection, token: str, book: dict) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO live_books (token_id, t, bid, ask, bid_size, ask_size, levels) VALUES (?,?,?,?,?,?,?)",
            (token, time.time(), book["bid"], book["ask"], book["bid_size"], book["ask_size"],
             json.dumps({"bids": book["bids"][:10], "asks": book["asks"][:10]})),
        )
