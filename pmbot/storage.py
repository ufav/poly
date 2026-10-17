import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "cs2.sqlite"
SPORTS = ("cs2", "dota2")             # esports: Polymarket + cybersport / PandaScore / OpenDota
ALL_SPORTS = (*SPORTS, "tennis")     # Polymarket collectors and market-only analysis


def db_path(sport: str = "cs2") -> Path:
    """One database per game, same schema (CS2 analysis assumes its base holds only CS2)."""
    return DEFAULT_DB.with_name(f"{sport}.sqlite")

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

-- taker trades (Data API /trades?takerOnly=true): side/price of the taker for `asset`
CREATE TABLE IF NOT EXISTS pm_trades (
    condition_id TEXT,
    asset        TEXT,
    t            INTEGER,
    side         TEXT,
    price        REAL,
    size         REAL,
    tx           TEXT,
    UNIQUE (condition_id, tx, asset, side, price, size)
);
CREATE INDEX IF NOT EXISTS pm_trades_cond_t ON pm_trades(condition_id, t);

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

-- players who actually played the match (cybersport lineup); steam_id = OpenDota account_id (Dota)
CREATE TABLE IF NOT EXISTS cs_match_players (
    match_id      TEXT,
    side          INTEGER,
    team_id       TEXT,
    player_id     TEXT,
    nickname      TEXT,
    country_code  TEXT,
    steam_id      TEXT,
    status        TEXT,
    position      INTEGER,
    PRIMARY KEY (match_id, side, player_id)
) WITHOUT ROWID;

-- PandaScore match (any game): schedule, result, tournament; raw = full JSON without streams
CREATE TABLE IF NOT EXISTS ps_match_info (
    match_id               INTEGER PRIMARY KEY,
    name                   TEXT,
    status                 TEXT,
    match_type             TEXT,
    number_of_games        INTEGER,
    scheduled_at           TEXT,
    original_scheduled_at  TEXT,
    rescheduled            INTEGER,
    begin_at               TEXT,
    end_at                 TEXT,
    forfeit                INTEGER,
    winner_id              INTEGER,
    team1_id               INTEGER,
    team2_id               INTEGER,
    score1                 INTEGER,
    score2                 INTEGER,
    tournament_id          INTEGER,
    serie_id               INTEGER,
    league_id              INTEGER,
    raw                    TEXT
);
CREATE INDEX IF NOT EXISTS ps_match_info_begin ON ps_match_info(begin_at);

-- tournament (stage) with serie and league; type online/offline, country, region, tier s/a/b/c/d
CREATE TABLE IF NOT EXISTS ps_tournaments (
    id            INTEGER PRIMARY KEY,
    name          TEXT,
    type          TEXT,
    country       TEXT,
    region        TEXT,
    tier          TEXT,
    prizepool     TEXT,
    begin_at      TEXT,
    end_at        TEXT,
    has_bracket   INTEGER,
    serie_id      INTEGER,
    serie_name    TEXT,
    serie_year    INTEGER,
    league_id     INTEGER,
    league_name   TEXT
);

CREATE TABLE IF NOT EXISTS ps_teams (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    acronym     TEXT,
    slug        TEXT,
    location    TEXT,
    fetched_at  TEXT
);

-- current roster at fetch time (PandaScore free tier has no history)
CREATE TABLE IF NOT EXISTS ps_players (
    id           INTEGER PRIMARY KEY,
    name         TEXT,
    first_name   TEXT,
    last_name    TEXT,
    nationality  TEXT,
    role         TEXT,
    age          INTEGER,
    birthday     TEXT,
    active       INTEGER,
    team_id      INTEGER
);

-- OpenDota league game (one Dota map); JSON columns: per-minute radiant advantage, objectives with time
CREATE TABLE IF NOT EXISTS od_matches (
    match_id                 INTEGER PRIMARY KEY,
    start_time               INTEGER,
    duration                 INTEGER,
    radiant_win              INTEGER,
    leagueid                 INTEGER,
    series_id                INTEGER,
    series_type              INTEGER,
    radiant_team_id          INTEGER,
    dire_team_id             INTEGER,
    radiant_team_name        TEXT,
    dire_team_name           TEXT,
    radiant_score            INTEGER,
    dire_score               INTEGER,
    radiant_captain          INTEGER,
    dire_captain             INTEGER,
    first_blood_time         INTEGER,
    tower_status_radiant     INTEGER,
    tower_status_dire        INTEGER,
    barracks_status_radiant  INTEGER,
    barracks_status_dire     INTEGER,
    cluster                  INTEGER,
    game_mode                INTEGER,
    lobby_type               INTEGER,
    version                  INTEGER,
    patch                    TEXT,
    radiant_gold_adv         TEXT,
    radiant_xp_adv           TEXT,
    objectives               TEXT,
    draft_timings            TEXT,
    fetched_at               TEXT
);
CREATE INDEX IF NOT EXISTS od_matches_start ON od_matches(start_time);

-- draft: team 0 = radiant, 1 = dire
CREATE TABLE IF NOT EXISTS od_picks_bans (
    match_id  INTEGER,
    ord       INTEGER,
    is_pick   INTEGER,
    hero_id   INTEGER,
    team      INTEGER,
    PRIMARY KEY (match_id, ord)
) WITHOUT ROWID;

-- player_slot < 128 = radiant; *_t = cumulative value per minute
CREATE TABLE IF NOT EXISTS od_players (
    match_id                 INTEGER,
    player_slot              INTEGER,
    account_id               INTEGER,
    hero_id                  INTEGER,
    kills                    INTEGER,
    deaths                   INTEGER,
    assists                  INTEGER,
    last_hits                INTEGER,
    denies                   INTEGER,
    gold_per_min             INTEGER,
    xp_per_min               INTEGER,
    gold_spent               INTEGER,
    net_worth                INTEGER,
    hero_damage              INTEGER,
    tower_damage             INTEGER,
    hero_healing             INTEGER,
    level                    INTEGER,
    stuns                    REAL,
    lane                     INTEGER,
    lane_role                INTEGER,
    is_roaming               INTEGER,
    teamfight_participation  REAL,
    towers_killed            INTEGER,
    roshans_killed           INTEGER,
    obs_placed               INTEGER,
    sen_placed               INTEGER,
    camps_stacked            INTEGER,
    creeps_stacked           INTEGER,
    rune_pickups             INTEGER,
    firstblood_claimed       INTEGER,
    leaver_status            INTEGER,
    item_0 INTEGER, item_1 INTEGER, item_2 INTEGER, item_3 INTEGER, item_4 INTEGER, item_5 INTEGER,
    backpack_0 INTEGER, backpack_1 INTEGER, backpack_2 INTEGER, item_neutral INTEGER,
    gold_t                   TEXT,
    xp_t                     TEXT,
    lh_t                     TEXT,
    dn_t                     TEXT,
    times                    TEXT,
    PRIMARY KEY (match_id, player_slot)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS od_players_account ON od_players(account_id);

-- zlib JSON: teamfights + per-player logs (kills, purchases, buybacks, runes, wards, ...) — opendota.load_logs
CREATE TABLE IF NOT EXISTS od_logs (
    match_id  INTEGER PRIMARY KEY,
    data      BLOB
);

-- Steam game ids OpenDota answered 404 for (not retried)
CREATE TABLE IF NOT EXISTS od_missing (match_id INTEGER PRIMARY KEY, fetched_at TEXT);

CREATE TABLE IF NOT EXISTS od_leagues (leagueid INTEGER PRIMARY KEY, name TEXT, tier TEXT);
CREATE TABLE IF NOT EXISTS od_teams (
    team_id INTEGER PRIMARY KEY, name TEXT, tag TEXT, rating REAL, wins INTEGER, losses INTEGER, last_match_time INTEGER);
CREATE TABLE IF NOT EXISTS od_pro_players (
    account_id INTEGER PRIMARY KEY, name TEXT, country_code TEXT, fantasy_role INTEGER, team_id INTEGER,
    team_name TEXT, team_tag TEXT, is_pro INTEGER);
CREATE TABLE IF NOT EXISTS od_heroes (
    id INTEGER PRIMARY KEY, name TEXT, localized_name TEXT, primary_attr TEXT, attack_type TEXT, roles TEXT);
CREATE TABLE IF NOT EXISTS od_items (id INTEGER PRIMARY KEY, name TEXT, cost INTEGER);
CREATE TABLE IF NOT EXISTS od_patches (id INTEGER PRIMARY KEY, name TEXT, date TEXT);

-- Dota map links: OpenDota game -> Polymarket event (game_id) and map number, cybersport match
CREATE TABLE IF NOT EXISTS dota_map_links (
    od_match_id  INTEGER PRIMARY KEY,
    pm_game_id   INTEGER,
    map_number   INTEGER,
    cs_match_id  TEXT,
    method       TEXT
);
CREATE INDEX IF NOT EXISTS dota_map_links_pm ON dota_map_links(pm_game_id, map_number);

-- tennis: Olimpbet results (pmbot/olimpbet.py); tournament name parsed to level / city / country / surface
CREATE TABLE IF NOT EXISTS ob_tournaments (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    country_id  INTEGER,
    level       TEXT,
    city        TEXT,
    country     TEXT,
    surface     TEXT,
    doubles     INTEGER,
    women       INTEGER
);
CREATE TABLE IF NOT EXISTS ob_events (
    id                 INTEGER PRIMARY KEY,
    tournament_id      INTEGER,
    day                TEXT,
    scheduled_at       TEXT,
    home_id            INTEGER,
    home               TEXT,
    home_country_id    INTEGER,
    away_id            INTEGER,
    away               TEXT,
    away_country_id    INTEGER,
    sets_home          INTEGER,
    sets_away          INTEGER,
    set_scores         TEXT,
    status             TEXT,
    completion_status  TEXT,
    event_type         TEXT,
    raw                TEXT
);
CREATE INDEX IF NOT EXISTS ob_events_day ON ob_events(day);
CREATE TABLE IF NOT EXISTS ob_days (day TEXT PRIMARY KEY, events INTEGER, pages INTEGER, fetched_at TEXT);

-- tennis: tennis-data.co.uk ATP / WTA main tour (pmbot/tennisdata.py); odds JSON: B365W/L, PSW/L, MaxW/L, AvgW/L, BFEW/L
CREATE TABLE IF NOT EXISTS td_matches (
    tour        TEXT,
    day         TEXT,
    location    TEXT,
    tournament  TEXT,
    series      TEXT,
    court       TEXT,
    surface     TEXT,
    round       TEXT,
    best_of     INTEGER,
    winner      TEXT,
    loser       TEXT,
    wrank       REAL,
    lrank       REAL,
    wpts        REAL,
    lpts        REAL,
    set_scores  TEXT,
    wsets       INTEGER,
    lsets       INTEGER,
    comment     TEXT,
    odds        TEXT,
    PRIMARY KEY (tour, day, winner, loser)
) WITHOUT ROWID;

-- tennis: Polymarket ↔ Olimpbet ↔ tennis-data + features (pmbot/tennis_join.py)
CREATE TABLE IF NOT EXISTS tennis_links (
    pm_event_id     TEXT PRIMARY KEY,
    ob_event_id     INTEGER,
    td_tour         TEXT,
    td_day          TEXT,
    td_winner       TEXT,
    td_loser        TEXT,
    method          TEXT,
    side_flip       INTEGER,
    surface         TEXT,
    court           TEXT,
    level           TEXT,
    city            TEXT,
    country         TEXT,
    round           TEXT,
    best_of         INTEGER,
    a_rank          REAL,
    b_rank          REAL,
    a_country       TEXT,
    b_country       TEXT,
    venue_country   TEXT,
    a_home          INTEGER,
    b_home          INTEGER,
    a_jetlag        REAL,
    b_jetlag        REAL,
    a_matches_7d    INTEGER,
    b_matches_7d    INTEGER,
    a_sets_7d       INTEGER,
    b_sets_7d       INTEGER,
    a_matches_14d   INTEGER,
    b_matches_14d   INTEGER,
    a_sets_14d      INTEGER,
    b_sets_14d      INTEGER,
    odds            TEXT,
    volume          REAL
);
CREATE INDEX IF NOT EXISTS tennis_links_ob ON tennis_links(ob_event_id);
CREATE INDEX IF NOT EXISTS tennis_links_td ON tennis_links(td_tour, td_day, td_winner, td_loser);
"""

# columns added after the first release: (table, column, type)
MIGRATIONS = [
    ("cs_matches", "team1_id", "TEXT"),
    ("cs_matches", "team2_id", "TEXT"),
    ("cs_matches", "location", "TEXT"),
    ("cs_matches", "stage_since", "TEXT"),
    ("cs_matches", "stage_till", "TEXT"),
    ("pm_markets", "trades_status", "TEXT"),
    ("cs_matches", "prize_pool", "INTEGER"),
    ("cs_matches", "bracket", "TEXT"),
    ("cs_matches", "round_name", "TEXT"),
    ("ob_days", "failed_hours", "INTEGER"),
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
