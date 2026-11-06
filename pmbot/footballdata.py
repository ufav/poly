"""football-data.co.uk: free CSV of football results, match stats and bookmaker odds (1X2, O/U 2.5, Asian handicap,
opening and closing; Pinnacle closing from 2012/13).

- main leagues, one file per season: /mmz4281/{season}/{div}.csv, season '9394' ... '2627' (start/end year), columns
  Div, Date, Time, HomeTeam, AwayTeam, FTHG, FTAG, FTR, HTHG, HTAG, HTR, Referee, [HxG, AxG from 2026/27], HS, AS,
  HST, AST, HF, AF, HC, AC, HY, AY, HR, AR, then odds (B365H ... PSCH, AvgC>2.5, AHCh ...);
- extra countries, all seasons in one file: /new/{code}.csv, columns Country, League, Season, Date, Time, Home, Away,
  HG, AG, Res, closing odds PSCH/MaxCH/AvgCH/BFECH/B365CH (1X2 only).
No Champions League / Europa League. Every odds column is kept in `odds` (JSON); the common ones are also columns.
"""

import csv
import datetime as dt
import io
import json
import sqlite3
from pathlib import Path

BASE = "https://www.football-data.co.uk"
DB = Path(__file__).resolve().parents[1] / "data" / "football.sqlite"

MAIN_DIVS = {
    "E0": ("England", "Premier League"), "E1": ("England", "Championship"), "E2": ("England", "League One"),
    "E3": ("England", "League Two"), "EC": ("England", "National League"),
    "SC0": ("Scotland", "Premiership"), "SC1": ("Scotland", "Championship"), "SC2": ("Scotland", "League One"),
    "SC3": ("Scotland", "League Two"),
    "D1": ("Germany", "Bundesliga"), "D2": ("Germany", "2. Bundesliga"),
    "I1": ("Italy", "Serie A"), "I2": ("Italy", "Serie B"),
    "SP1": ("Spain", "La Liga"), "SP2": ("Spain", "Segunda"),
    "F1": ("France", "Ligue 1"), "F2": ("France", "Ligue 2"),
    "N1": ("Netherlands", "Eredivisie"), "B1": ("Belgium", "Pro League"), "P1": ("Portugal", "Primeira Liga"),
    "T1": ("Turkey", "Super Lig"), "G1": ("Greece", "Super League"),
}
EXTRA = ("ARG", "AUT", "BRA", "CHN", "DNK", "FIN", "IRL", "JPN", "MEX", "NOR", "POL", "ROU", "RUS", "SWE", "SWZ",
         "USA")
FIRST_SEASON = 1993

SCHEMA = """
CREATE TABLE IF NOT EXISTS fd_matches (
    div      TEXT,      -- E0 ... / extra country code (BRA)
    season   INTEGER,   -- start year (2025 = 2025/26; calendar-year leagues: the year)
    country  TEXT,
    league   TEXT,
    date     TEXT,      -- YYYY-MM-DD
    time     TEXT,      -- HH:MM UK time, when given
    home     TEXT,
    away     TEXT,
    fthg INTEGER, ftag INTEGER, ftr TEXT, hthg INTEGER, htag INTEGER, htr TEXT,
    hxg REAL, axg REAL,
    hs INTEGER, as_ INTEGER, hst INTEGER, ast INTEGER, hf INTEGER, af INTEGER, hc INTEGER, ac INTEGER,
    hy INTEGER, ay INTEGER, hr INTEGER, ar INTEGER,
    referee  TEXT,
    psh REAL, psd REAL, psa REAL,          -- Pinnacle (opening-ish, at file update)
    psch REAL, pscd REAL, psca REAL,       -- Pinnacle closing
    avgch REAL, avgcd REAL, avgca REAL,    -- market average closing
    pc_over25 REAL, pc_under25 REAL,       -- Pinnacle closing O/U 2.5
    avgc_over25 REAL, avgc_under25 REAL,
    odds     TEXT,      -- JSON: every odds column of the file
    PRIMARY KEY (div, date, home, away)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS fd_matches_season ON fd_matches (div, season);
CREATE TABLE IF NOT EXISTS fd_files (
    path       TEXT PRIMARY KEY,  -- mmz4281/2526/E0.csv | new/BRA.csv
    status     TEXT,              -- done / missing / error
    rows       INTEGER,
    fetched_at REAL
);
"""

# file column -> table column (main / extra files)
_COLS = {
    "HomeTeam": "home", "AwayTeam": "away", "Home": "home", "Away": "away",
    "FTHG": "fthg", "FTAG": "ftag", "FTR": "ftr", "HG": "fthg", "AG": "ftag", "Res": "ftr",
    "HTHG": "hthg", "HTAG": "htag", "HTR": "htr", "HxG": "hxg", "AxG": "axg",
    "HS": "hs", "AS": "as_", "HST": "hst", "AST": "ast", "HF": "hf", "AF": "af", "HC": "hc", "AC": "ac",
    "HY": "hy", "AY": "ay", "HR": "hr", "AR": "ar", "Referee": "referee",
    "PSH": "psh", "PSD": "psd", "PSA": "psa", "PSCH": "psch", "PSCD": "pscd", "PSCA": "psca",
    "AvgCH": "avgch", "AvgCD": "avgcd", "AvgCA": "avgca",
    "PC>2.5": "pc_over25", "PC<2.5": "pc_under25", "AvgC>2.5": "avgc_over25", "AvgC<2.5": "avgc_under25",
}
_INT = {"fthg", "ftag", "hthg", "htag", "hs", "as_", "hst", "ast", "hf", "af", "hc", "ac", "hy", "ay", "hr", "ar"}
_TEXT = {"home", "away", "ftr", "htr", "referee"}
_NOT_ODDS = {"Div", "Date", "Time", "Country", "League", "Season", *(k for k, v in _COLS.items()
                                                                     if v in _INT | _TEXT | {"hxg", "axg"})}
FIELDS = ["div", "season", "country", "league", "date", "time", *dict.fromkeys(_COLS.values()), "odds"]


def connect(path: Path = DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def season_code(start_year: int) -> str:
    """2025 -> '2526'."""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def current_season(today: dt.date | None = None) -> int:
    """Start year of the European season running on `today` (July switch)."""
    today = today or dt.date.today()
    return today.year if today.month >= 7 else today.year - 1


def files(since: int = FIRST_SEASON, extra: bool = True) -> list[tuple[str, str, int | None]]:
    """(path, div, season start year; None for the all-seasons extra files) of everything to fetch."""
    out = [(f"mmz4281/{season_code(y)}/{div}.csv", div, y)
           for y in range(since, current_season() + 1) for div in MAIN_DIVS]
    if extra:
        out += [(f"new/{code}.csv", code, None) for code in EXTRA]
    return out


def _date(value: str) -> str | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _num(value: str, kind: type):
    try:
        return kind(float(value)) if kind is int else float(value)
    except (TypeError, ValueError):
        return None


def parse_csv(raw: bytes, div: str, season: int | None) -> list[dict]:
    """Rows of one file -> fd_matches dicts; rows without teams / date / full-time score are skipped."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    country, league = MAIN_DIVS.get(div, (None, None))
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k and k.strip()}
        date = _date(row.get("Date", ""))
        rec = {"div": div, "season": season, "country": row.get("Country") or country,
               "league": row.get("League") or league, "date": date, "time": row.get("Time") or None}
        if season is None:  # extra files: '2012' or '2012/2013'
            rec["season"] = _num((row.get("Season") or "").split("/")[0], int)
        odds = {}
        for k, v in row.items():
            col = _COLS.get(k)
            if col:
                rec[col] = (v or None) if col in _TEXT else _num(v, int if col in _INT else float)
            if k not in _NOT_ODDS and v:
                x = _num(v, float)
                if x is not None:
                    odds[k] = x
        if not rec.get("home") or not rec.get("away") or not date or rec.get("fthg") is None:
            continue
        rec["odds"] = json.dumps(odds, separators=(",", ":"))
        out.append(rec)
    return out


def save(conn: sqlite3.Connection, path: str, rows: list[dict], status: str, now: float) -> None:
    with conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO fd_matches ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})",
            [tuple(r.get(f) for f in FIELDS) for r in rows])
        conn.execute("INSERT OR REPLACE INTO fd_files (path, status, rows, fetched_at) VALUES (?,?,?,?)",
                     (path, status, len(rows), now))
