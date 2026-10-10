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

CREATE TABLE IF NOT EXISTS cs_matches (
    id            TEXT PRIMARY KEY,
    date          TEXT,
    time          TEXT,
    format        TEXT,
    is_finished   INTEGER,
    team1         TEXT,
    team2         TEXT,
    score1        INTEGER,
    score2        INTEGER,
    tournament    TEXT,
    tier          INTEGER,
    external_id   INTEGER,
    stats_team1   TEXT,
    stats_team2   TEXT,
    begin_at      INTEGER,
    odds          TEXT,
    stats_status  TEXT,
    fetched_at    TEXT
);
CREATE INDEX IF NOT EXISTS cs_matches_external ON cs_matches(external_id);
CREATE INDEX IF NOT EXISTS cs_matches_date ON cs_matches(date);

CREATE TABLE IF NOT EXISTS cs_games (
    id           TEXT PRIMARY KEY,
    match_id     TEXT REFERENCES cs_matches(id),
    number       INTEGER,
    map_name     TEXT,
    score1       INTEGER,
    score2       INTEGER,
    external_id  TEXT,
    rounds       INTEGER
);
CREATE INDEX IF NOT EXISTS cs_games_match ON cs_games(match_id);

CREATE TABLE IF NOT EXISTS cs_rounds (
    game_id      TEXT,
    round        INTEGER,
    ct_team      TEXT,
    t_team       TEXT,
    winner_side  TEXT,
    winner_team  TEXT,
    outcome      TEXT,
    PRIMARY KEY (game_id, round)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS cs_teams (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    fetched_at  TEXT
);

CREATE TABLE IF NOT EXISTS cs_team_players (
    team_id       TEXT,
    player_id     TEXT,
    nickname      TEXT,
    country_code  TEXT,
    status        TEXT,
    since         TEXT,
    till          TEXT,
    PRIMARY KEY (team_id, player_id, since)
) WITHOUT ROWID;

-- player stats per map from cybersport match-stats; game_id = cs_games.id; team via cs_team_players at match date
CREATE TABLE IF NOT EXISTS cs_player_stats (
    game_id    TEXT,
    player_id  TEXT,
    nickname   TEXT,
    kills      INTEGER,
    deaths     INTEGER,
    assists    INTEGER,
    adr        REAL,
    kast       REAL,
    rating     REAL,
    PRIMARY KEY (game_id, player_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS cs_player_stats_status (
    match_id   TEXT PRIMARY KEY,
    players    INTEGER,
    fetched_at TEXT
);

-- PandaScore map timing; match_id = pm_events.game_id, position = map number
CREATE TABLE IF NOT EXISTS ps_games (
    match_id   INTEGER,
    position   INTEGER,
    game_id    INTEGER,
    status     TEXT,
    begin_at   TEXT,
    end_at     TEXT,
    length     INTEGER,
    winner_id  INTEGER,
    PRIMARY KEY (match_id, position)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ps_matches (
    match_id    INTEGER PRIMARY KEY,
    status      TEXT,
    fetched_at  TEXT
);

CREATE TABLE IF NOT EXISTS cs_countries (
    code   TEXT PRIMARY KEY,
    title  TEXT
);
"""

# columns added after the first release: (table, column, type)
MIGRATIONS = [
    ("cs_matches", "team1_id", "TEXT"),
    ("cs_matches", "team2_id", "TEXT"),
    ("cs_matches", "location", "TEXT"),
    ("cs_matches", "stage_since", "TEXT"),
    ("cs_matches", "stage_till", "TEXT"),
]


def connect(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=120)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for table, column, kind in MIGRATIONS:
        if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.commit()
    return conn
