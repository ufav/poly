"""Link cybersport.ru matches to Polymarket events.

1. cs_matches.external_id = pm_events.game_id (only matches with round stats, ~40%);
2. otherwise the same pair of normalized team names (with learned aliases) and start within ±1 day.
"""

import datetime as dt
import re
import sqlite3
import unicodedata
from collections import defaultdict

_NOISE = {"team", "esports", "esport", "gaming", "club", "gg", "the", "e-sports"}


def norm_team(name: str | None) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w and w not in _NOISE]
    return "".join(words)


def team_aliases(conn: sqlite3.Connection) -> dict[str, str]:
    """Normalized cybersport name -> normalized Polymarket name, learned from exact (external_id) links
    where the other team's name already matches."""
    alias = {}
    for t1, t2, a, b in conn.execute(
            """SELECT c.team1, c.team2, e.team_a, e.team_b FROM cs_matches c
               JOIN pm_events e ON e.game_id = c.external_id"""):
        n1, n2, na, nb = map(norm_team, (t1, t2, a, b))
        for same, other, pm_other in ((n1, n2, nb if n1 == na else na), (n2, n1, nb if n2 == na else na)):
            if same in (na, nb) and other not in (na, nb):
                alias[other] = pm_other
    return alias


def same_team(cs_name: str | None, pm_name: str | None, alias: dict[str, str]) -> bool:
    n = norm_team(cs_name)
    return norm_team(pm_name) in (n, alias.get(n, n))


def link_matches(conn: sqlite3.Connection, use_external: bool = True) -> dict[str, str]:
    """cs_match_id -> pm_event_id."""
    events_by_game = {g: e for e, g in conn.execute("SELECT id, game_id FROM pm_events WHERE game_id IS NOT NULL")}
    events_by_pair = defaultdict(list)
    for event_id, a, b, start in conn.execute("SELECT id, team_a, team_b, start_time FROM pm_events"):
        if a and b and start:
            day = dt.date.fromisoformat(start[:10])
            events_by_pair[frozenset((norm_team(a), norm_team(b)))].append((day, event_id))
    alias = team_aliases(conn)

    links = {}
    for cs_id, ext, t1, t2, date in conn.execute("SELECT id, external_id, team1, team2, date FROM cs_matches"):
        if use_external and ext and ext in events_by_game:
            links[cs_id] = events_by_game[ext]
            continue
        n1, n2 = norm_team(t1), norm_team(t2)
        candidates = (events_by_pair.get(frozenset((n1, n2)), [])
                      + events_by_pair.get(frozenset((alias.get(n1, n1), alias.get(n2, n2))), []))
        day = dt.date.fromisoformat(date[:10])
        near = [(abs((d - day).days), e) for d, e in candidates if abs((d - day).days) <= 1]
        if near:
            links[cs_id] = min(near)[1]
    return links
