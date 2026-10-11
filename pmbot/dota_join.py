"""Link Dota 2 maps across sources: Polymarket event (game_id) + map number <-> OpenDota game <-> cybersport match.

1. `ps_length`: PandaScore map (match_id = Polymarket game_id, position = map number) <-> OpenDota game with the same
   duration (PandaScore `length` = OpenDota `duration` to the second) that ended up to 3 h before PandaScore `end_at`
   (it lags by 5-25 min); among several — one with a matching team name, then the closest. Without a name match
   the link is kept only if the duration is unique in the window (`ps_length_only`).
2. `cybersport`: cybersport map `external_id` = Steam id = OpenDota game; the cybersport match is linked to Polymarket
   by team names ± 1 day (pmbot.join) or through games already linked in step 1; map number = cybersport order.
cs_matches.external_id is set to the Polymarket game_id (= PandaScore id, as for CS2).
"""

import bisect
import datetime as dt
import sqlite3
from collections import Counter, defaultdict

from pmbot.join import link_matches, norm_team

WINDOW = 3 * 3600
LAG = 600


def _ts(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _od_games(conn: sqlite3.Connection) -> dict[int, list[tuple]]:
    """duration -> [(end, match_id, {normalized team names})]."""
    by_dur = defaultdict(list)
    for mid, end, dur, r, d in conn.execute(
            """SELECT m.match_id, m.start_time + m.duration, m.duration, COALESCE(m.radiant_team_name, rt.name),
                      COALESCE(m.dire_team_name, dt.name)
               FROM od_matches m LEFT JOIN od_teams rt ON rt.team_id = m.radiant_team_id
                                 LEFT JOIN od_teams dt ON dt.team_id = m.dire_team_id"""):
        by_dur[dur].append((end, mid, {norm_team(r), norm_team(d)} - {""}))
    for v in by_dur.values():
        v.sort()
    return by_dur


def _by_length(conn: sqlite3.Connection) -> dict[int, tuple]:
    by_dur = _od_games(conn)
    ps_names = defaultdict(set)
    for mid, name in conn.execute("""SELECT i.match_id, t.name FROM ps_match_info i
                                     JOIN ps_teams t ON t.id IN (i.team1_id, i.team2_id)"""):
        ps_names[mid].add(norm_team(name))
    links = {}
    for game_id, pos, end_at, length, a, b in conn.execute(
            """SELECT g.match_id, g.position, g.end_at, g.length, e.team_a, e.team_b FROM ps_games g
               JOIN pm_events e ON e.game_id = g.match_id WHERE g.end_at IS NOT NULL AND g.length > 0"""):
        t = _ts(end_at)
        names = ({norm_team(a), norm_team(b)} | ps_names[game_id]) - {""}
        cands = []
        for dur in range(length - 2, length + 3):
            games = by_dur.get(dur, [])
            for end, mid, od_names in games[bisect.bisect_left(games, (t - WINDOW,)):]:
                if end > t + LAG:
                    break
                cands.append((not (od_names & names), abs(end - t), mid))
        if not cands:
            continue
        cands.sort()
        no_name, _, mid = cands[0]
        if no_name and len(cands) > 1:
            continue
        links[mid] = (game_id, pos, "ps_length_only" if no_name else "ps_length")
    return links


def link_maps(conn: sqlite3.Connection) -> Counter:
    """Rebuild dota_map_links; returns counts by method and conflicts between the two routes."""
    links = _by_length(conn)
    stats = Counter(m for _, _, m in links.values())
    od_ids = {r[0] for r in conn.execute("SELECT match_id FROM od_matches")}
    events = dict(conn.execute("SELECT id, game_id FROM pm_events WHERE game_id IS NOT NULL"))
    cs_games = defaultdict(list)
    for cs_id, number, ext in conn.execute(
            "SELECT match_id, number, external_id FROM cs_games WHERE external_id GLOB '[0-9]*' ORDER BY match_id, number"):
        cs_games[cs_id].append((number, int(ext)))

    # cybersport match -> Polymarket game_id: majority of its maps already linked by length, else by names
    by_names = {cs: events.get(e) for cs, e in link_matches(conn, use_external=False).items()}
    cs_link = {}
    for cs_id, games in cs_games.items():
        votes = Counter(links[ext][0] for _, ext in games if ext in links)
        cs_link[cs_id] = votes.most_common(1)[0][0] if votes else by_names.get(cs_id)
    cs_of_od = {ext: cs_id for cs_id, games in cs_games.items() for _, ext in games}

    for cs_id, games in cs_games.items():
        game_id = cs_link.get(cs_id)
        if not game_id:
            continue
        for number, ext in games:
            if ext in links:
                stats["agree" if links[ext][:2] == (game_id, number) else "conflict"] += 1
            elif ext in od_ids:
                links[ext] = (game_id, number, "cybersport")
                stats["cybersport"] += 1

    with conn:
        conn.execute("DELETE FROM dota_map_links")
        conn.executemany(
            "INSERT INTO dota_map_links (od_match_id, pm_game_id, map_number, cs_match_id, method) VALUES (?,?,?,?,?)",
            [(mid, g, n, cs_of_od.get(mid), m) for mid, (g, n, m) in links.items()])
        conn.executemany("UPDATE cs_matches SET external_id=? WHERE id=?",
                         [(g, cs) for cs, g in cs_link.items() if g])
    return stats
