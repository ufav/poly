"""Olimp results archive (monthly xlsx, 2008-03 ... 2022-10, all sports; columns tournament, teams, time, values):
football rows -> data/football.sqlite.

- match row: 'Футбол. Англия. Премьер-лига' / 'Арсенал - Тоттенхэм' / '01.10.2022 17:30:00' /
  '3:1 (1:1)Первый гол: Арсенал на 20 минутеВторой гол: ...Угловые: 14, A: 9, B: 5Удары в створ: ...'
  (details vary: goal minutes from ~2012, corners / shots / possession / cards mostly for top events);
- stat row ('... Статистика', 'УГЛ A - УГЛ B', 'A (фолы) - B (фолы)', 'Специальные ставки', 'Показатели игроков'):
  one stat of a match, kept apart in ol_stats;
- time: 'dd.mm.yyyy HH:MM:SS' (site time as published) or an Excel serial day number.
No odds. Cyber football is skipped.
"""

import datetime as dt
import json
import re
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS ol_matches (
    dt          TEXT,     -- YYYY-MM-DD HH:MM (time as in the archive)
    tournament  TEXT,
    home        TEXT,
    away        TEXT,
    fthg INTEGER, ftag INTEGER, hthg INTEGER, htag INTEGER,
    periods     TEXT,     -- all period scores in brackets, e.g. '1:1' or '0:0 1:1' (extra time)
    goals       TEXT,     -- JSON [[minute, "h"|"a"], ...] of the goals named: 1st-4th + 'Последний гол'
    goals_complete INTEGER, -- 1 = every goal of the score is listed (often only the first / last are)
    hc INTEGER, ac INTEGER,          -- corners
    hst INTEGER, ast INTEGER,        -- shots on target
    hoff INTEGER, aoff INTEGER,      -- offsides
    hposs INTEGER, aposs INTEGER,    -- possession %
    yellow      INTEGER,  -- 'Предупреждения' total
    red         INTEGER,  -- 'Удаления': 1 yes / 0 no
    penalty     INTEGER,  -- 'Пенальти': 1 yes / 0 no
    added1 INTEGER, added2 INTEGER,  -- added time, minutes
    raw         TEXT,
    PRIMARY KEY (tournament, home, away, dt)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ol_matches_dt ON ol_matches (dt);
CREATE TABLE IF NOT EXISTS ol_stats (
    dt          TEXT,
    tournament  TEXT,     -- without '. Статистика'
    stat        TEXT,     -- УГЛ / ЖК / фолы / удары в створ / 5 минут / спец. ставки ...
    home        TEXT,     -- team names without the stat label
    away        TEXT,
    hval REAL, aval REAL, hht REAL, aht REAL,
    raw         TEXT,
    PRIMARY KEY (tournament, stat, home, away, dt)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ol_files (
    name TEXT PRIMARY KEY, size INTEGER, matches INTEGER, stats INTEGER, imported_at REAL
);
"""

_SCORE = re.compile(r"^\s*(\d+):(\d+)\s*(?:\(([\d:\s]+)\))?")
_LAST_GOAL = re.compile(r"Последний гол: (.+?) на (\d+) минуте")
_GOAL = re.compile(r"(?:Первый|Второй|Третий|Четвертый|Пятый|Шестой|Седьмой|Восьмой|Девятый|Десятый|\d+-й) гол: "
                   r"(.+?) на (\d+) минуте")
_PREFIX = re.compile(r"^(УГЛ|ЖК|ФОЛ|ОФС|УДС|УД|АУТ|ВР) ")
_SUFFIX = re.compile(r"^(.*\S)\s+\(([^()]+)\)$")
_STAT_TOURN = (". Статистика", ". Специальные ставки", ". Показатели игроков")


def connect_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def parse_time(value: str) -> str | None:
    value = (value or "").strip()
    if re.fullmatch(r"\d+(\.\d+)?", value):  # Excel serial day
        d = dt.datetime(1899, 12, 30) + dt.timedelta(days=float(value))
        return d.strftime("%Y-%m-%d %H:%M")
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(value, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return None


def _split_teams(teams: str) -> tuple[str, str] | None:
    """'A - B'; a team number with a dash ('Флора - 2 - Вильянди', 'Вильянди - Флора - 2') is joined back."""
    parts = [p.strip() for p in (teams or "").split(" - ")]
    if len(parts) == 3:
        if parts[1].isdigit():
            parts = [f"{parts[0]} - {parts[1]}", parts[2]]
        elif parts[2].isdigit():
            parts = [parts[0], f"{parts[1]} - {parts[2]}"]
    return (parts[0], parts[1]) if len(parts) == 2 and all(parts) else None


def _pair(text: str, label: str, home: str, away: str) -> tuple[int | None, int | None]:
    """'Label: total, HOME: x, AWAY: y' or 'Label: HOME: x%, AWAY: y%' -> (x, y)."""
    i = text.find(label + ":")
    if i < 0:
        return None, None
    chunk = text[i + len(label) + 1:i + len(label) + 1 + 160]
    out = []
    for team in (home, away):
        m = re.search(re.escape(team) + r": (\d+)", chunk)
        out.append(int(m.group(1)) if m else None)
    return out[0], out[1]


def _yes_no(text: str, label: str) -> int | None:
    m = re.search(re.escape(label) + r": (да|нет)", text)
    return None if not m else int(m.group(1) == "да")


def parse_match(home: str, away: str, values: str) -> dict:
    v = values or ""
    rec: dict = {"raw": v}
    m = _SCORE.match(v)
    if m:
        rec["fthg"], rec["ftag"] = int(m.group(1)), int(m.group(2))
        if m.group(3):
            periods = m.group(3).split()
            rec["periods"] = " ".join(periods)
            ht = periods[0].split(":")
            if len(ht) == 2 and all(x.isdigit() for x in ht):
                rec["hthg"], rec["htag"] = int(ht[0]), int(ht[1])
    goals = []
    for team, minute in _GOAL.findall(v):
        side = "h" if team.strip() == home else "a" if team.strip() == away else None
        if side:
            goals.append([int(minute), side])
    last = _LAST_GOAL.search(v)
    if last and "fthg" in rec and len(goals) < rec["fthg"] + rec["ftag"]:  # ordinals stop at the 4th goal
        side = "h" if last.group(1).strip() == home else "a" if last.group(1).strip() == away else None
        if side:
            goals.append([int(last.group(2)), side])
    if goals:
        rec["goals"] = json.dumps(goals, separators=(",", ":"))
    if "fthg" in rec:
        rec["goals_complete"] = int(len(goals) == rec["fthg"] + rec["ftag"])
    rec["hc"], rec["ac"] = _pair(v, "Угловые", home, away)
    rec["hst"], rec["ast"] = _pair(v, "Удары в створ", home, away)
    rec["hoff"], rec["aoff"] = _pair(v, "Офсайды", home, away)
    rec["hposs"], rec["aposs"] = _pair(v, "Процент владения мячом", home, away)
    y = re.search(r"Предупреждения: (\d+)", v)
    rec["yellow"] = int(y.group(1)) if y else None
    rec["red"], rec["penalty"] = _yes_no(v, "Удаления"), _yes_no(v, "Пенальти")
    a1 = re.search(r"1-м тайме: (\d+) мин", v)
    a2 = re.search(r"2-м тайме: (\d+) мин", v)
    rec["added1"], rec["added2"] = (int(a1.group(1)) if a1 else None), (int(a2.group(1)) if a2 else None)
    return rec


def _stat_label(home: str, away: str) -> tuple[str, str, str] | None:
    """('УГЛ A', 'УГЛ B') / ('A (фолы)', 'B (фолы)') -> (stat, A, B)."""
    ph, pa = _PREFIX.match(home), _PREFIX.match(away)
    if ph and pa and ph.group(1) == pa.group(1):
        return ph.group(1), home[ph.end():].strip(), away[pa.end():].strip()
    sh, sa = _SUFFIX.match(home), _SUFFIX.match(away)
    if sh and sa and sh.group(2) == sa.group(2):
        return sh.group(2), sh.group(1), sa.group(1)
    return None


def classify(row: dict) -> tuple[str, dict] | None:
    """Archive row -> ('match', ol_matches record) / ('stat', ol_stats record) / None (not football / unusable)."""
    tourn = (row.get("tournament") or "").strip()
    if not tourn.startswith("Футбол.") or "Кибер" in tourn:
        return None
    teams = _split_teams(row.get("teams"))
    when = parse_time(str(row.get("time") or ""))
    if not teams or not when:
        return None
    home, away = teams
    values = str(row.get("values") or "")
    stat_tourn = next((s for s in _STAT_TOURN if tourn.endswith(s)), None)
    label = _stat_label(home, away)
    if label and not stat_tourn and not _PREFIX.match(home):
        label = None  # '(жен)', '(мол)', '(19)', '(дубль)' outside stat tournaments: real teams
    if label or stat_tourn:
        stat, h, a = label or (stat_tourn.strip(". "), home, away)
        base = tourn[: -len(". Статистика")] if tourn.endswith(". Статистика") else tourn
        m = _SCORE.match(values)
        rec = {"dt": when, "tournament": base, "stat": stat, "home": h, "away": a, "raw": values}
        if m:
            rec["hval"], rec["aval"] = float(m.group(1)), float(m.group(2))
            ht = (m.group(3) or "").split()[:1]
            if ht and ht[0].count(":") == 1:
                x, y = ht[0].split(":")
                if x.isdigit() and y.isdigit():
                    rec["hht"], rec["aht"] = float(x), float(y)
        return "stat", rec
    rec = parse_match(home, away, values)
    rec.update({"dt": when, "tournament": tourn, "home": home, "away": away})
    return "match", rec


MATCH_FIELDS = ["dt", "tournament", "home", "away", "fthg", "ftag", "hthg", "htag", "periods", "goals",
                "goals_complete", "hc", "ac",
                "hst", "ast", "hoff", "aoff", "hposs", "aposs", "yellow", "red", "penalty", "added1", "added2", "raw"]
STAT_FIELDS = ["dt", "tournament", "stat", "home", "away", "hval", "aval", "hht", "aht", "raw"]


def save(conn: sqlite3.Connection, matches: list[dict], stats: list[dict]) -> None:
    with conn:
        conn.executemany(f"INSERT OR REPLACE INTO ol_matches ({', '.join(MATCH_FIELDS)}) "
                         f"VALUES ({', '.join('?' * len(MATCH_FIELDS))})",
                         [tuple(r.get(f) for f in MATCH_FIELDS) for r in matches])
        conn.executemany(f"INSERT OR REPLACE INTO ol_stats ({', '.join(STAT_FIELDS)}) "
                         f"VALUES ({', '.join('?' * len(STAT_FIELDS))})",
                         [tuple(r.get(f) for f in STAT_FIELDS) for r in stats])
