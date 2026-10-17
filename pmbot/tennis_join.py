"""Link Polymarket tennis events to Olimpbet results and tennis-data.co.uk matches.

Name formats:
  Polymarket  First Last     (Tristan Boyer, Alex de Minaur)
  Olimpbet    Last First     (Boyer Tristan, De Minaur Alex)
  tennis-data Last I.        (Boyer T., De Minaur A.)

Matching: soft-fold (ae/oe/ue → a/o/u for German transliteration), token frozenset equality
or subset (Barrios Vera Tomas ⊇ Tomas Barrios), day ±1. Olimpbet: MAIN singles only
(no Statistics / Doubles / Mixed).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict

from pmbot import geo

# iso3 from Polymarket flag URLs → iso2
ISO3 = {
    "afg": "af", "alb": "al", "alg": "dz", "and": "ad", "arg": "ar", "arm": "am", "aus": "au",
    "aut": "at", "aze": "az", "bah": "bs", "ban": "bd", "bar": "bb", "bel": "be", "ben": "bj",
    "ber": "bm", "bih": "ba", "blr": "by", "bol": "bo", "bot": "bw", "bra": "br", "bru": "bn",
    "bul": "bg", "bur": "bf", "bdi": "bi", "cam": "kh", "can": "ca", "chi": "cl", "chn": "cn",
    "civ": "ci", "cmr": "cm", "cod": "cd", "cog": "cg", "col": "co", "crc": "cr", "cro": "hr",
    "cub": "cu", "cyp": "cy", "cze": "cz", "den": "dk", "deu": "de", "dom": "do", "ecu": "ec",
    "egy": "eg", "esa": "sv", "esp": "es", "est": "ee", "eth": "et", "fiji": "fj", "fin": "fi",
    "fra": "fr", "gab": "ga", "gbr": "gb", "geo": "ge", "ger": "de", "gha": "gh", "gre": "gr",
    "grn": "gd", "gua": "gt", "gui": "gn", "guy": "gy", "hai": "ht", "hkg": "hk", "hon": "hn",
    "hun": "hu", "ina": "id", "ind": "in", "iri": "ir", "irl": "ie", "irq": "iq", "isl": "is",
    "isr": "il", "ita": "it", "jam": "jm", "jor": "jo", "jpn": "jp", "kaz": "kz", "ken": "ke",
    "kgz": "kg", "kor": "kr", "ksa": "sa", "kuw": "kw", "lat": "lv", "lba": "ly", "lbr": "lr",
    "les": "ls", "lib": "lb", "lie": "li", "ltu": "lt", "lux": "lu", "mad": "mg", "mar": "ma",
    "mas": "my", "mda": "md", "mex": "mx", "mkd": "mk", "mli": "ml", "mlt": "mt", "mne": "me",
    "mon": "mc", "moz": "mz", "mri": "mu", "myanmar": "mm", "nam": "na", "nca": "ni", "ned": "nl",
    "nep": "np", "nga": "ng", "nig": "ne", "nor": "no", "nzl": "nz", "oma": "om", "pak": "pk",
    "pan": "pa", "par": "py", "per": "pe", "phi": "ph", "pol": "pl", "por": "pt", "prk": "kp",
    "pur": "pr", "qat": "qa", "rou": "ro", "rsa": "za", "rus": "ru", "rwa": "rw", "sam": "ws",
    "sen": "sn", "sey": "sc", "sin": "sg", "sko": "sk", "sle": "sl", "slo": "si", "smr": "sm",
    "sol": "sb", "som": "so", "srb": "rs", "sri": "lk", "sud": "sd", "sui": "ch", "sur": "sr",
    "svk": "sk", "swe": "se", "swz": "sz", "syr": "sy", "tan": "tz", "tha": "th", "tjk": "tj",
    "tkm": "tm", "tog": "tg", "tpe": "tw", "tri": "tt", "tun": "tn", "tur": "tr", "uae": "ae",
    "uga": "ug", "ukr": "ua", "uru": "uy", "usa": "us", "uzb": "uz", "van": "vu", "ven": "ve",
    "vie": "vn", "vin": "vc", "yem": "ye", "zam": "zm", "zim": "zw",
}

# Olimpbet / tennis-data English country title → iso2
COUNTRY_TITLE = {
    "usa": "us", "united states": "us", "u.s.a.": "us", "u.s.": "us",
    "uk": "gb", "united kingdom": "gb", "great britain": "gb", "england": "gb",
    "scotland": "gb", "wales": "gb", "northern ireland": "gb",
    "russia": "ru", "russian federation": "ru",
    "czech republic": "cz", "czechia": "cz", "slovakia": "sk",
    "south korea": "kr", "korea": "kr", "north korea": "kp",
    "taiwan": "tw", "chinese taipei": "tw", "hong kong": "hk", "macau": "mo",
    "uae": "ae", "united arab emirates": "ae",
    "bosnia": "ba", "bosnia and herzegovina": "ba", "bosnia & herzegovina": "ba",
    "north macedonia": "mk", "macedonia": "mk",
    "ivory coast": "ci", "cote divoire": "ci", "côte d'ivoire": "ci",
    "dominican republic": "do", "el salvador": "sv", "puerto rico": "pr",
    "new zealand": "nz", "south africa": "za", "saudi arabia": "sa",
    "trinidad and tobago": "tt", "cabo verde": "cv", "cape verde": "cv",
}


def soft(s: str) -> str:
    """ASCII fold + German digraphs (Moeller → moller, Schwaerzler → schwarzler)."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return s.replace("oe", "o").replace("ae", "a").replace("ue", "u")


def tokens(name: str | None) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", soft(name or "")) if w and w not in {"jr", "sr"}]


_VS = re.compile(r":\s*(.+?)\s+vs\.?\s+(.+)$", re.I)

# slam / finals without city.surface in Olimpbet tournament name
VENUE_BY_LEVEL = {
    "australian open": ("Melbourne", "Australia", "Hard"),
    "roland garros": ("Paris", "France", "Clay"),
    "french open": ("Paris", "France", "Clay"),
    "wimbledon": ("London", "Great Britain", "Grass"),
    "us open": ("New York", "USA", "Hard"),
    "atp finals": ("Turin", "Italy", "Hard"),
    "atp world tour finals": ("Turin", "Italy", "Hard"),
    "wta finals": ("Riyadh", "Saudi Arabia", "Hard"),
}


def event_players(title: str | None, team_a: str | None, team_b: str | None) -> tuple[str | None, str | None]:
    """Prefer full names from 'Tournament: A vs B' when team_a/team_b are surnames only."""
    if team_a and team_b and len(tokens(team_a)) >= 2 and len(tokens(team_b)) >= 2:
        return team_a, team_b
    m = _VS.search(title or "")
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return team_a, team_b


def fill_venue(level: str | None, city: str | None, country: str | None,
               surface: str | None) -> tuple[str | None, str | None, str | None]:
    if city and country and surface:
        return city, country, surface
    key = soft(level or "")
    for name, (c, co, s) in VENUE_BY_LEVEL.items():
        if name in key or key.startswith(name):
            return city or c, country or co, surface or s
    return city, country, surface


def name_key(name: str | None) -> frozenset[str] | None:
    t = tokens(name)
    return frozenset(t) if t else None


def match_player(pm: str | None, ob: str | None) -> bool:
    a, b = name_key(pm), name_key(ob)
    if not a or not b:
        return False
    if a == b:
        return True
    if a <= b or b <= a:
        smaller = a if len(a) <= len(b) else b
        if len(smaller) >= 2:
            return True
        only = next(iter(smaller))
        return len(only) >= 5
    return False


def pm_surnames(name: str | None) -> set[str]:
    t = tokens(name)
    if not t:
        return set()
    out = {t[-1]}
    if len(t) >= 3:
        out.add("".join(t[1:]))
        out.add("".join(t[-2:]))
    return out


def ob_surnames(name: str | None) -> set[str]:
    """Olimpbet is Last First — first token is the surname; also keep longest token."""
    t = tokens(name)
    if not t:
        return set()
    out = {t[0], max(t, key=len)}
    if len(t) >= 3:
        out.add("".join(t[:2]))
    return out


def td_player_key(name: str | None) -> tuple[str, str] | None:
    """'De Minaur A.' → ('deminaur', 'a'); 'Alcaraz C.' → ('alcaraz', 'c')."""
    t = tokens(name)
    if not t:
        return None
    if len(t[-1]) == 1:
        return ("".join(t[:-1]), t[-1])
    return ("".join(t[1:]) if len(t) > 1 else t[0], t[0][0])


def pm_td_keys(name: str | None) -> set[tuple[str, str]]:
    t = tokens(name)
    if not t:
        return set()
    init = t[0][0]
    keys = {(t[-1], init), ("".join(t[1:]), init)}
    if len(t) >= 3:
        keys.add(("".join(t[-2:]), init))
    return keys


_FLAG_RE = re.compile(r"country-flags/([a-z]{3})\.png", re.I)


def pm_countries(raw: str | None) -> tuple[str | None, str | None]:
    """iso2 for team_a, team_b from event JSON teams[].logo."""
    if not raw:
        return None, None
    try:
        doc = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return None, None
    teams = doc.get("teams") or []
    codes = []
    for team in teams[:2]:
        logo = (team or {}).get("logo") or ""
        m = _FLAG_RE.search(logo)
        codes.append(ISO3.get(m.group(1).lower()) if m else None)
    while len(codes) < 2:
        codes.append(None)
    return codes[0], codes[1]


def country_iso(name: str | None) -> str | None:
    if not name:
        return None
    s = soft(name).strip()
    if s in COUNTRY_TITLE:
        return COUNTRY_TITLE[s]
    if len(s) == 2 and s in geo.COUNTRY_UTC:
        return s
    # title-case country already in COUNTRY_UTC keys? try first letters
    for title, code in COUNTRY_TITLE.items():
        if title.startswith(s) or s.startswith(title):
            return code
    # common full English names matching iso via known map built from COUNTRY_UTC labels
    EN = {
        "france": "fr", "germany": "de", "spain": "es", "italy": "it", "portugal": "pt",
        "switzerland": "ch", "austria": "at", "netherlands": "nl", "belgium": "be",
        "sweden": "se", "norway": "no", "denmark": "dk", "finland": "fi", "poland": "pl",
        "romania": "ro", "hungary": "hu", "greece": "gr", "turkey": "tr", "croatia": "hr",
        "serbia": "rs", "slovenia": "si", "bulgaria": "bg", "ukraine": "ua", "belarus": "by",
        "china": "cn", "japan": "jp", "india": "in", "indonesia": "id", "thailand": "th",
        "vietnam": "vn", "malaysia": "my", "singapore": "sg", "philippines": "ph",
        "australia": "au", "canada": "ca", "mexico": "mx", "brazil": "br", "argentina": "ar",
        "chile": "cl", "colombia": "co", "peru": "pe", "ecuador": "ec", "uruguay": "uy",
        "egypt": "eg", "morocco": "ma", "tunisia": "tn", "algeria": "dz", "nigeria": "ng",
        "kazakhstan": "kz", "uzbekistan": "uz", "israel": "il", "qatar": "qa", "kuwait": "kw",
        "lebanon": "lb", "jordan": "jo", "iran": "ir", "iraq": "iq", "pakistan": "pk",
        "ireland": "ie", "luxembourg": "lu", "monaco": "mc", "malta": "mt", "cyprus": "cy",
        "georgia": "ge", "armenia": "am", "azerbaijan": "az", "moldova": "md", "lithuania": "lt",
        "latvia": "lv", "estonia": "ee", "iceland": "is",
    }
    return EN.get(s)


def venue_offset(city: str | None, country: str | None) -> float | None:
    return geo.city_utc(city) or geo.COUNTRY_UTC.get(country_iso(country) or "")


def _ob_rows(conn: sqlite3.Connection) -> list[tuple]:
    return list(conn.execute(
        """SELECT e.id, e.day, e.home, e.away, e.home_id, e.away_id,
                  e.sets_home, e.sets_away, e.set_scores, e.scheduled_at,
                  t.level, t.city, t.country, t.surface, t.name
           FROM ob_events e
           JOIN ob_tournaments t ON t.id = e.tournament_id
           WHERE e.event_type = 'MAIN' AND IFNULL(t.doubles, 0) = 0
             AND IFNULL(t.name, '') NOT LIKE '%Statistics%'
             AND IFNULL(t.name, '') NOT LIKE '%Mixed%'"""
    ))


def link_ob(conn: sqlite3.Connection, window: int = 1) -> dict[str, dict]:
    """pm_event_id → {ob_event_id, method, side_flip, ... tournament fields}."""
    by_day: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for row in _ob_rows(conn):
        eid, day, home, away = row[0], row[1], row[2], row[3]
        for sur in ob_surnames(home) | ob_surnames(away):
            by_day[day][sur].append(row)

    links: dict[str, dict] = {}
    for pm_id, title, a, b, start, raw in conn.execute(
            "SELECT id, title, team_a, team_b, start_time, raw FROM pm_events"):
        a, b = event_players(title, a, b)
        if not a or not b or not start:
            continue
        day = dt.date.fromisoformat(start[:10])
        surs = pm_surnames(a) | pm_surnames(b)
        cands: list[tuple[str, tuple]] = []  # method, row
        seen: set[int] = set()
        for d in range(-window, window + 1):
            ds = (day + dt.timedelta(days=d)).isoformat()
            bucket = by_day.get(ds)
            if not bucket:
                continue
            rows = []
            for sur in surs:
                rows.extend(bucket.get(sur, []))
            for row in rows:
                oid = row[0]
                if oid in seen:
                    continue
                home, away = row[2], row[3]
                if match_player(a, home) and match_player(b, away):
                    method = "exact" if name_key(a) == name_key(home) and name_key(b) == name_key(away) else "soft"
                    cands.append((method, 0, row))
                    seen.add(oid)
                elif match_player(a, away) and match_player(b, home):
                    method = "exact" if name_key(a) == name_key(away) and name_key(b) == name_key(home) else "soft"
                    cands.append((method, 1, row))
                    seen.add(oid)
        if not cands:
            continue
        # prefer exact, then unique
        exact = [c for c in cands if c[0] == "exact"]
        pool = exact or cands
        if len({c[2][0] for c in pool}) != 1:
            continue
        method, flip, row = pool[0]
        city, country, surface = fill_venue(row[10], row[11], row[12], row[13])
        links[pm_id] = {
            "ob_event_id": row[0],
            "ob_day": row[1],
            "home": row[2], "away": row[3],
            "home_id": row[4], "away_id": row[5],
            "sets_home": row[6], "sets_away": row[7],
            "set_scores": row[8], "scheduled_at": row[9],
            "level": row[10], "city": city, "country": country, "surface": surface,
            "tournament": row[14],
            "method": method, "side_flip": flip,
        }
    return links


def link_td(conn: sqlite3.Connection, window: int = 1) -> dict[str, dict]:
    """pm_event_id → {td_tour, td_day, td_winner, td_loser, ...}."""
    by_day: dict[str, list] = defaultdict(list)
    for row in conn.execute(
            """SELECT tour, day, winner, loser, location, tournament, series, court, surface,
                      round, best_of, wrank, lrank, wpts, lpts, odds
               FROM td_matches"""):
        kw, kl = td_player_key(row[2]), td_player_key(row[3])
        if kw and kl and kw != kl:
            by_day[row[1]].append((frozenset((kw, kl)), row))

    links: dict[str, dict] = {}
    for pm_id, title, a, b, start, series in conn.execute(
            """SELECT id, title, team_a, team_b, start_time, json_extract(raw, '$.seriesSlug')
               FROM pm_events"""):
        a, b = event_players(title, a, b)
        if not a or not b or not start:
            continue
        keys_a, keys_b = pm_td_keys(a), pm_td_keys(b)
        if not keys_a or not keys_b:
            continue
        day = dt.date.fromisoformat(start[:10])
        cands = []
        for d in range(-window, window + 1):
            ds = (day + dt.timedelta(days=d)).isoformat()
            for pair, row in by_day.get(ds, []):
                tour = row[0]
                if series in ("atp", "wta") and tour != series:
                    continue
                hit = any(frozenset((ka, kb)) == pair for ka in keys_a for kb in keys_b)
                if hit:
                    cands.append(row)
        cands = list({(r[0], r[1], r[2], r[3]): r for r in cands}.values())
        if len(cands) != 1:
            continue
        r = cands[0]
        a_is_winner = td_player_key(r[2]) in keys_a
        links[pm_id] = {
            "td_tour": r[0], "td_day": r[1], "td_winner": r[2], "td_loser": r[3],
            "td_location": r[4], "td_tournament": r[5], "td_series": r[6],
            "court": r[7], "td_surface": r[8], "round": r[9], "best_of": r[10],
            "wrank": r[11], "lrank": r[12], "wpts": r[13], "lpts": r[14], "odds": r[15],
            "a_is_winner": int(bool(a_is_winner)),
        }
    return links


def _player_history(conn: sqlite3.Connection) -> dict[int, list[tuple[str, str | None, str | None, int]]]:
    """player_id → sorted [(day, city, country, sets_played), ...] from MAIN singles."""
    hist: dict[int, list] = defaultdict(list)
    for day, home_id, away_id, sh, sa, city, country, level in conn.execute(
            """SELECT e.day, e.home_id, e.away_id, e.sets_home, e.sets_away, t.city, t.country, t.level
               FROM ob_events e JOIN ob_tournaments t ON t.id = e.tournament_id
               WHERE e.event_type = 'MAIN' AND IFNULL(t.doubles, 0) = 0
                 AND IFNULL(t.name, '') NOT LIKE '%Statistics%'
                 AND e.home_id IS NOT NULL AND e.away_id IS NOT NULL
               ORDER BY e.day"""):
        city, country, _ = fill_venue(level, city, country, None)
        sets = (sh or 0) + (sa or 0)
        hist[home_id].append((day, city, country, sets))
        hist[away_id].append((day, city, country, sets))
    return hist


def _fatigue(hist: list, day: str, days: int) -> tuple[int, int]:
    end = dt.date.fromisoformat(day)
    start = end - dt.timedelta(days=days)
    n_m = n_s = 0
    for d, _c, _co, sets in hist:
        dd = dt.date.fromisoformat(d)
        if start <= dd < end:
            n_m += 1
            n_s += sets
    return n_m, n_s


def _prev_venue(hist: list, day: str) -> tuple[str | None, str | None]:
    for d, city, country, _ in reversed(hist):
        if d < day:
            return city, country
    return None, None


def save_links(conn: sqlite3.Connection) -> dict[str, int]:
    """Rebuild tennis_links with OB/TD ids and derived features. Returns counts."""
    conn.execute("DELETE FROM tennis_links")
    ob = link_ob(conn)
    td = link_td(conn)
    hist = _player_history(conn)

    n = 0
    for pm_id, title, a, b, start, raw, volume in conn.execute(
            "SELECT id, title, team_a, team_b, start_time, raw, volume FROM pm_events"):
        o = ob.get(pm_id)
        t = td.get(pm_id)
        if not o and not t:
            continue
        a, b = event_players(title, a, b)
        a_cc, b_cc = pm_countries(raw)
        surface = (o or {}).get("surface") or (t or {}).get("td_surface")
        city = (o or {}).get("city") or (t or {}).get("td_location")
        country = (o or {}).get("country")
        if t and not country:
            # tennis-data location is city; country unknown — leave None
            pass
        city, country, surface = fill_venue((o or {}).get("level") or (t or {}).get("td_tournament"),
                                           city, country, surface)
        venue_cc = country_iso(country)
        level = (o or {}).get("level") or (t or {}).get("td_series")
        court = (t or {}).get("court")
        round_ = (t or {}).get("round")
        best_of = (t or {}).get("best_of")

        a_rank = b_rank = None
        odds = None
        if t:
            odds = t.get("odds")
            if t.get("a_is_winner"):
                a_rank, b_rank = t.get("wrank"), t.get("lrank")
            else:
                a_rank, b_rank = t.get("lrank"), t.get("wrank")

        a_home = int(a_cc == venue_cc) if a_cc and venue_cc else None
        b_home = int(b_cc == venue_cc) if b_cc and venue_cc else None

        a_jetlag = b_jetlag = None
        a_m7 = a_s7 = b_m7 = b_s7 = a_m14 = a_s14 = b_m14 = b_s14 = None
        method = None
        flip = None
        ob_id = None
        if o:
            ob_id = o["ob_event_id"]
            method = o["method"]
            flip = o["side_flip"]
            # map PM A/B → OB player ids
            if flip:
                id_a, id_b = o["away_id"], o["home_id"]
            else:
                id_a, id_b = o["home_id"], o["away_id"]
            day = o["ob_day"]
            cur_off = venue_offset(city, country)

            def _side(pid: int | None) -> tuple:
                if not pid:
                    return None, None, None, None, None
                h = hist.get(pid) or []
                m7v, s7v = _fatigue(h, day, 7)
                m14v, s14v = _fatigue(h, day, 14)
                pc, pco = _prev_venue(h, day)
                po = venue_offset(pc, pco)
                jet = geo.shift_hours(cur_off, po) if cur_off is not None and po is not None else None
                return jet, m7v, s7v, m14v, s14v

            a_jetlag, a_m7, a_s7, a_m14, a_s14 = _side(id_a)
            b_jetlag, b_m7, b_s7, b_m14, b_s14 = _side(id_b)
        if t and not method:
            method = "td"
        elif t and method:
            method = method + "+td"

        conn.execute(
            """INSERT INTO tennis_links (
                pm_event_id, ob_event_id, td_tour, td_day, td_winner, td_loser,
                method, side_flip, surface, court, level, city, country, round, best_of,
                a_rank, b_rank, a_country, b_country, venue_country,
                a_home, b_home, a_jetlag, b_jetlag,
                a_matches_7d, b_matches_7d, a_sets_7d, b_sets_7d,
                a_matches_14d, b_matches_14d, a_sets_14d, b_sets_14d, odds, volume
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pm_id, ob_id,
                (t or {}).get("td_tour"), (t or {}).get("td_day"),
                (t or {}).get("td_winner"), (t or {}).get("td_loser"),
                method, flip, surface, court, level, city, country, round_, best_of,
                a_rank, b_rank, a_cc, b_cc, venue_cc,
                a_home, b_home, a_jetlag, b_jetlag,
                a_m7, b_m7, a_s7, b_s7, a_m14, b_m14, a_s14, b_s14,
                odds, volume,
            ),
        )
        n += 1
    conn.commit()
    n_ob = conn.execute("SELECT COUNT(*) FROM tennis_links WHERE ob_event_id IS NOT NULL").fetchone()[0]
    n_td = conn.execute("SELECT COUNT(*) FROM tennis_links WHERE td_tour IS NOT NULL").fetchone()[0]
    n_both = conn.execute(
        "SELECT COUNT(*) FROM tennis_links WHERE ob_event_id IS NOT NULL AND td_tour IS NOT NULL"
    ).fetchone()[0]
    return {"links": n, "with_ob": n_ob, "with_td": n_td, "with_both": n_both}
