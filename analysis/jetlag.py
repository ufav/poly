"""Jet lag and home crowd vs the pre-match Polymarket price.

Team home = median time zone of player nationalities on the roster at match date (cybersport.ru),
venue = tournament location (empty = online). For each LAN match the team with the larger time shift
is the "traveler"; we compare its win rate with its price at scheduled start.

  python analysis/jetlag.py [--min-volume 2000]
"""

import argparse
import datetime as dt
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import geo, storage  # noqa: E402
from pmbot.join import link_matches, same_team, team_aliases  # noqa: E402


def pre_match_price(conn, token: str, start_time: str) -> float | None:
    start = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
    row = conn.execute("SELECT p FROM pm_prices WHERE token_id = ? AND t <= ? ORDER BY t DESC LIMIT 1",
                       (token, start)).fetchone()
    return row[0] if row and 0 < row[0] < 1 else None


def bucket_shift(h: float) -> str:
    return "0-2h" if h <= 2 else "3-5h" if h <= 5 else "6h+"


def bucket_day(d: int | None) -> str:
    return "day ?" if d is None else "day 0-1" if d <= 1 else "day 2-4" if d <= 4 else "day 5+"


def report(title: str, data: dict) -> None:
    print(title)
    print(f"   {'group':48} {'n':>5} {'avg price':>10} {'win rate':>9} {'edge':>7} {'±2se':>6}")
    for key in sorted(data):
        n, w, s = data[key]
        if n >= 20:
            win = w / n
            print(f"   {key:48} {n:5d} {s / n:10.3f} {win:9.1%} {win - s / n:+7.3f} {2 * (win * (1 - win) / n) ** 0.5:6.3f}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-volume", type=float, default=2000)
    args = ap.parse_args()
    conn = storage.connect()
    links = link_matches(conn)
    alias = team_aliases(conn)
    titles = geo.country_titles(conn)

    traveler = defaultdict(lambda: [0, 0, 0.0])
    home = defaultdict(lambda: [0, 0, 0.0])
    coverage = Counter()
    unknown_venues = Counter()
    for cs_id, event_id in links.items():
        row = conn.execute(
            """SELECT c.date, c.team1, c.team2, c.team1_id, c.team2_id, c.location, c.stage_since,
                      e.start_time, m.token_a, m.outcome_a, m.final_price_a, m.volume
               FROM cs_matches c JOIN pm_events e ON e.id = ?
               JOIN pm_markets m ON m.event_id = e.id AND m.market_type = 'moneyline'
               WHERE c.id = ? AND m.closed = 1 AND m.final_price_a IN (0, 1) AND m.history_status = 'done'""",
            (event_id, cs_id)).fetchone()
        if not row:
            continue
        date, t1, t2, id1, id2, location, since, start, token, outcome_a, final_a, volume = row
        if volume < args.min_volume or not id1 or not id2:
            continue
        if same_team(t1, outcome_a, alias):
            first_is_a = True
        elif same_team(t2, outcome_a, alias):
            first_is_a = False
        else:
            coverage["outcome not matched"] += 1
            continue
        p_a = pre_match_price(conn, token, start)
        if p_a is None:
            continue
        p1 = p_a if first_is_a else 1 - p_a
        won1 = (final_a == 1) == first_is_a
        coverage["priced"] += 1

        if geo.is_online(location):
            coverage["online"] += 1
            continue
        venue = geo.venue_utc(location, titles)
        if venue is None:
            unknown_venues[location] += 1
            continue
        h1, h2 = geo.team_home_utc(conn, id1, date), geo.team_home_utc(conn, id2, date)
        if h1 is None or h2 is None:
            coverage["LAN, roster unknown"] += 1
            continue
        coverage["LAN, analysed"] += 1
        s1, s2 = geo.shift_hours(h1, venue), geo.shift_hours(h2, venue)
        day = (dt.date.fromisoformat(date) - dt.date.fromisoformat(since)).days if since else None

        # traveler perspective: team with the larger shift
        if s1 != s2:
            p_t, won_t, s_t, s_o, h_t = (p1, won1, s1, s2, h1) if s1 > s2 else (1 - p1, not won1, s2, s1, h2)
            diff = s_t - s_o
            diff_b = "diff 1-3h" if diff <= 3 else "diff 4-6h" if diff <= 6 else "diff 7h+"
            keys = [f"{diff_b}", f"{diff_b} / {bucket_day(day)}",
                    f"traveler {bucket_shift(s_t)} vs other {bucket_shift(s_o)}"]
            if diff >= 4:
                region = "Americas" if h_t < -2 else "Asia/Oceania" if h_t > 4.5 else "Europe/CIS/MENA"
                direction = "east" if (venue - h_t) % 24 < 12 else "west"
                keys += [f"diff 4h+ / traveler {'favorite' if p_t >= 0.5 else 'underdog'}",
                         f"diff 4h+ / traveler from {region}", f"diff 4h+ / flew {direction}"]
            for k in keys:
                a = traveler[k]
                a[0] += 1
                a[1] += won_t
                a[2] += p_t

        # home crowd: venue country = majority nationality of exactly one team
        vc = geo.venue_country(location, titles)
        c1 = Counter(geo.roster_countries(conn, id1, date)).most_common(1)
        c2 = Counter(geo.roster_countries(conn, id2, date)).most_common(1)
        home1 = bool(c1) and c1[0][0] == vc and c1[0][1] >= 3
        home2 = bool(c2) and c2[0][0] == vc and c2[0][1] >= 3
        if home1 != home2:
            p_h, won_h = (p1, won1) if home1 else (1 - p1, not won1)
            for k in ("home team (all)", f"home team {'favorite' if p_h >= 0.5 else 'underdog'}"):
                a = home[k]
                a[0] += 1
                a[1] += won_h
                a[2] += p_h

    print("coverage:", dict(coverage))
    if unknown_venues:
        print("venues without time zone (add to pmbot/geo.py):", unknown_venues.most_common(15))
    print()
    report("TRAVELER (larger time shift) price at start vs win rate", traveler)
    report("HOME TEAM (>=3 players from venue country) price at start vs win rate", home)


if __name__ == "__main__":
    main()
