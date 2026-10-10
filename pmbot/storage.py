import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "cs2.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_events (
    id            TEXT PRIMARY KEY,
    slug          TEXT,
    title         TEXT,
    game_id       INTEGER,
    start_time    TEXT,
    best_of       INTEGER,
    tournament    TEXT,
    team_a        TEXT,
    team_b        TEXT,
    score         TEXT,
    volume        REAL,
    liquidity     REAL,
    closed        INTEGER,
    raw           TEXT,
    fetched_at    TEXT
);
CREATE INDEX IF NOT EXISTS pm_events_game_id ON pm_events(game_id);
CREATE INDEX IF NOT EXISTS pm_events_start ON pm_events(start_time);

CREATE TABLE IF NOT EXISTS pm_markets (
    id              TEXT PRIMARY KEY,
    event_id        TEXT REFERENCES pm_events(id),
    condition_id    TEXT,
    market_type     TEXT,
    group_title     TEXT,
    map_number      INTEGER,
    outcome_a       TEXT,
    outcome_b       TEXT,
    token_a         TEXT,
    token_b         TEXT,
    final_price_a   REAL,
    volume          REAL,
    closed          INTEGER,
    history_status  TEXT
);
CREATE INDEX IF NOT EXISTS pm_markets_event ON pm_markets(event_id);

CREATE TABLE IF NOT EXISTS pm_prices (
    token_id  TEXT,
    t         INTEGER,
    p         REAL,
    PRIMARY KEY (token_id, t)
) WITHOUT ROWID;
"""


def connect(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
