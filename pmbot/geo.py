"""Time zones for jet lag: team home (majority of player nationalities) vs tournament venue.

Offsets are standard UTC hours without DST (±1 h error is fine for jet lag buckets).
Multi-zone countries use the zone where most teams / events are.
"""

import sqlite3
from statistics import median

COUNTRY_UTC = {
    # Europe
    "gb": 0, "ie": 0, "pt": 0, "is": 0,
    "fr": 1, "de": 1, "nl": 1, "be": 1, "lu": 1, "ch": 1, "at": 1, "it": 1, "es": 1, "dk": 1, "se": 1, "no": 1,
    "pl": 1, "cz": 1, "sk": 1, "hu": 1, "si": 1, "hr": 1, "ba": 1, "rs": 1, "me": 1, "mk": 1, "al": 1, "xk": 1, "mt": 1,
    "fi": 2, "ee": 2, "lv": 2, "lt": 2, "ua": 2, "ro": 2, "md": 2, "bg": 2, "gr": 2, "cy": 2,
    "by": 3, "ru": 3, "tr": 3,
    # Caucasus, Central Asia, Middle East, Africa
    "ge": 4, "am": 4, "az": 4, "ae": 4, "om": 4,
    "kz": 5, "uz": 5, "tj": 5, "pk": 5, "tm": 5,
    "kg": 6, "bd": 6,
    "il": 2, "eg": 2, "jo": 3, "lb": 2, "sy": 3, "iq": 3, "sa": 3, "kw": 3, "qa": 3, "bh": 3, "ir": 3.5,
    "za": 2, "ma": 1, "tn": 1, "dz": 1, "ng": 1, "ke": 3,
    # Asia, Oceania
    "in": 5.5, "np": 5.75, "lk": 5.5,
    "th": 7, "vn": 7, "id": 7, "kh": 7, "la": 7,
    "cn": 8, "hk": 8, "mo": 8, "tw": 8, "sg": 8, "my": 8, "ph": 8, "mn": 8,
    "jp": 9, "kr": 9,
    "au": 10, "nz": 12,
    # Americas
    "us": -6, "ca": -5, "mx": -6, "gt": -6, "cr": -6, "pa": -5, "co": -5, "pe": -5, "ec": -5,
    "ve": -4, "bo": -4, "cl": -4, "py": -4, "do": -4, "pr": -4,
    "br": -3, "ar": -3, "uy": -3,
}

# venue cities in multi-zone countries (lowercase, as written on cybersport.ru)
CITY_UTC = {
    "даллас": -6, "остин": -6, "чикаго": -6, "атланта": -5, "нью-йорк": -5, "вашингтон": -5, "майами": -5,
    "лас-вегас": -8, "лос-анджелес": -8, "сиэтл": -8, "сан-франциско": -8, "сан-диего": -8, "финикс": -7,
    "денвер": -7, "торонто": -5, "монреаль": -5, "ванкувер": -8,
    "москва": 3, "санкт-петербург": 3, "казань": 3, "сочи": 3, "екатеринбург": 5, "новосибирск": 7,
    "сидней": 10, "мельбурн": 10, "брисбен": 10, "перт": 8, "аделаида": 9.5,
    "рио-де-жанейро": -3, "сан-паулу": -3,
}


def is_online(location: str | None) -> bool:
    return not location or not location.strip() or "онлайн" in location.lower()


def venue_utc(location: str | None, country_codes: dict[str, str]) -> float | None:
    """'Город, Страна' -> UTC offset; None for online / unknown. country_codes: russian title -> iso2."""
    if is_online(location):
        return None
    parts = [p.strip() for p in location.split(",")]
    city = parts[0].lower()
    if city in CITY_UTC:
        return CITY_UTC[city]
    code = venue_country(location, country_codes)
    return COUNTRY_UTC.get(code) if code else None


# venue countries no player in the base comes from
EXTRA_COUNTRY_TITLES = {"мальта": "mt", "нидерланды": "nl", "катар": "qa", "оаэ": "ae", "сингапур": "sg"}


def country_titles(conn: sqlite3.Connection) -> dict[str, str]:
    titles = {title.lower(): code for code, title in conn.execute("SELECT code, title FROM cs_countries") if title}
    return {**EXTRA_COUNTRY_TITLES, **titles}


def venue_country(location: str | None, country_codes: dict[str, str]) -> str | None:
    if is_online(location):
        return None
    for part in reversed([p.strip() for p in location.split(",")]):
        if part.lower() in country_codes:
            return country_codes[part.lower()]
    return None


def shift_hours(a: float, b: float) -> float:
    d = abs(a - b) % 24
    return min(d, 24 - d)


def roster_countries(conn: sqlite3.Connection, team_id: str, date: str) -> list[str]:
    """Nationalities of players on the roster at `date` (coach excluded)."""
    return [c for (c,) in conn.execute(
        """SELECT country_code FROM cs_team_players
           WHERE team_id=? AND status IN ('player','substitute') AND since<=? AND (till IS NULL OR till>=?)""",
        (team_id, date, date)) if c]


def team_home_utc(conn: sqlite3.Connection, team_id: str, date: str) -> float | None:
    """Median offset of player nationalities on the roster at `date`."""
    offsets = [COUNTRY_UTC[c] for c in roster_countries(conn, team_id, date) if c in COUNTRY_UTC]
    return median(offsets) if offsets else None
