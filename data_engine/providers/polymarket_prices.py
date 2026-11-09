"""Polymarket match markets -> PostgreSQL (the betting venue, docs/md1.md 6.2): every run (worker, every 10 min)

1. discovery: main events (no parent event, with a moneyline) of the target series starting in the next DISCOVER_DAYS:
   match (status scheduled), league, teams - a team is matched by name / alias to a known team of the sport, else
   created - and markets: football 1x2 (three Yes/No markets: home / draw / away), other sports moneyline, spreads
   (line = home handicap) and totals; outcome tokens in markets.extra;
2. snapshots: order books (CLOB POST /books, batched) of the outcome tokens of matches starting within SNAPSHOT_DAYS
   or playing now -> odds_history (bid, ask, ask_size, price = mid, odds = 1/ask, taken_at); a row only when the
   best bid / ask / size changed since the last stored one.

Target series from the audit (analysis/pm_sports_audit.py): liquid football leagues and cups, ATP / WTA, NBA / WNBA,
NHL. Volleyball, ITF and European hockey / basketball are too thin.

  python -u -m data_engine.providers.polymarket_prices
  python -u -m data_engine.providers.polymarket_prices --history 60   # once: past games, for team mapping
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
import unicodedata
from decimal import Decimal

import httpx
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db import session
from core.models import League, Market, Match, OddsQuote, SourceEntity, Team
from pmbot.http import HttpClient
from pmbot.join import norm_team
from pmbot.polymarket import CLOB, GAMMA

SOURCE = 4  # polymarket
DISCOVER_DAYS = 7
SNAPSHOT_DAYS = 3
LIVE_HOURS = 4
SPORT_ID = {"football": 1, "tennis": 2, "basketball": 3, "hockey": 5}
# Gamma sport code -> (our sport, league country, league name of an existing league if any)
SERIES = {
    "epl": ("football", "England", "Premier League"), "lal": ("football", "Spain", "La Liga"),
    "sea": ("football", "Italy", "Serie A"), "bun": ("football", "Germany", "Bundesliga"),
    "fl1": ("football", "France", "Ligue 1"), "mls": ("football", "USA", "MLS"),
    "bra": ("football", "Brazil", "Serie A"), "mex": ("football", "Mexico", "Liga MX"),
    "ucl": ("football", None, "UEFA Champions League"), "uel": ("football", None, "UEFA Europa League"),
    "unl": ("football", None, "UEFA Nations League"), "efl": ("football", "England", "EFL Cup"),
    "atp": ("tennis", None, "ATP Tour"), "wta": ("tennis", None, "WTA Tour"),
    "nba": ("basketball", "USA", "NBA"), "wnba": ("basketball", "USA", "WNBA"), "nhl": ("hockey", "USA", "NHL"),
}
CLUB_WORDS = {"fc", "afc", "cf", "sc", "ac", "ssc", "fk", "sk", "bk", "cd", "ud", "sd", "rc", "rcd", "sv", "vfl",
              "vfb", "tsg", "1", "calcio", "hotspur", "albion", "and", "hove"}
GENDER = {"wta": "f", "wnba": "f"}
# sportsMarketType -> (our kind, period, outcome shape); period 0 = match, 1..5 = set / quarter, 11 / 12 = halves.
# shapes: sides (the two teams / players), sides_line (+ a handicap, stored as the home handicap), ou (over / under),
# team_ou (a team total: subject = home / away), prop (a player's Yes = over the line: subject = the player), yesno
KINDS = {
    "moneyline": ("moneyline", 0, "sides"), "spreads": ("spread", 0, "sides_line"), "totals": ("total", 0, "ou"),
    "tennis_match_totals": ("total_games", 0, "ou"),
    # football
    "both_teams_to_score": ("btts", 0, "yesno"),
    # basketball: halves, quarters, team totals, overtime, player props
    "first_half_moneyline": ("moneyline", 11, "sides"), "first_half_spreads": ("spread", 11, "sides_line"),
    "first_half_totals": ("total", 11, "ou"),
    **{f"q{q}_spreads": ("spread", q, "sides_line") for q in range(1, 5)},
    **{f"q{q}_totals": ("total", q, "ou") for q in range(1, 5)},
    "team_totals": ("team_total", 0, "team_ou"), "first_half_team_totals": ("team_total", 11, "team_ou"),
    "second_half_team_totals": ("team_total", 12, "team_ou"), "basketball_game_overtime": ("overtime", 0, "yesno"),
    **{t: (f"player_{t}", 0, "prop") for t in ("points", "rebounds", "assists", "threes", "steals", "blocks")},
    # tennis: sets
    "tennis_first_set_winner": ("moneyline", 1, "sides"), "tennis_set_winner": ("moneyline", None, "sides"),
    "tennis_set_handicap": ("set_spread", 0, "sides_line"), "tennis_game_handicap": ("spread", 0, "sides_line"),
    "tennis_set_totals": ("total_sets", 0, "ou"), "tennis_first_set_totals": ("total_games", 1, "ou"),
    "tennis_set_games_totals": ("total_games", None, "ou"),
}


def series_ids(http: HttpClient) -> dict[str, str]:
    return {x["sport"]: str(x.get("series") or "").split(",")[0] for x in http.get_json(f"{GAMMA}/sports")
            if x.get("sport") in SERIES and x.get("series")}


def kickoff(e: dict) -> dt.datetime:
    return dt.datetime.fromisoformat((e.get("startTime") or e["startDate"]).replace("Z", "+00:00"))


def open_events(http: HttpClient, series: str, now: dt.datetime) -> list[dict]:
    """Open events of a series whose game starts within [now - LIVE_HOURS, now + DISCOVER_DAYS]. Gamma's
    start_date_* filters use startDate (when the market opened), not the game time, so filter on startTime here."""
    out, offset = [], 0
    lo, hi = now - dt.timedelta(hours=LIVE_HOURS), now + dt.timedelta(days=DISCOVER_DAYS)
    while True:
        page = http.get_json(f"{GAMMA}/events", {"series_id": series, "closed": "false", "limit": 100,
                                                 "offset": offset})
        out += [e for e in page if lo <= kickoff(e) <= hi]
        if len(page) < 100 or offset >= 1900:
            return out
        offset += 100


def norm_name(name: str | None) -> str:
    """Team name key: norm_team without club-form words ('Tottenham Hotspur FC' -> 'tottenham')."""
    words = [w for w in re.split(r"[^a-z0-9]+", norm_team_words(name)) if w and w not in CLUB_WORDS]
    return "".join(words)


def name_key(sport_id: int, name: str | None) -> str:
    """Lookup key of a team name; tennis ignores the word order ('Elena Rybakina' = 'Rybakina Elena')."""
    if sport_id != 2:
        return norm_name(name)
    return "".join(sorted(w for w in re.split(r"[^a-z0-9]+", norm_team_words(name)) if w))


def norm_team_words(name: str | None) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"(the|club|team)", " ", s)


class Resolver:
    """Get-or-create of leagues / teams / matches for Polymarket events, with source_entities."""

    def __init__(self, s) -> None:
        self.s = s
        self.own = {(t, e): i for t, e, i in s.execute(
            select(SourceEntity.entity_type, SourceEntity.external_id, SourceEntity.entity_id)
            .where(SourceEntity.source_id == SOURCE))}
        # (sport, gender, name key) -> senior team ids known under that name by any source
        self.names: dict[tuple[int, str, str], set[int]] = {}
        for sport_id, gender, tid, name in s.execute(text(
                "SELECT t.sport_id, t.gender, t.id, n FROM teams t LEFT JOIN source_entities se "
                "ON se.entity_type = 'team' AND se.entity_id = t.id, LATERAL (VALUES (t.name), (se.external_name)) "
                "v(n) WHERE n IS NOT NULL AND t.sport_id IN (1, 2, 3, 5) AND t.age_group IS NULL")):
            self.names.setdefault((sport_id, gender, name_key(sport_id, name)), set()).add(tid)
        self.fd_teams = set(s.scalars(select(SourceEntity.entity_id).where(SourceEntity.source_id == 1,
                                                                           SourceEntity.entity_type == "team")))
        self.country = dict(s.execute(text("SELECT id, country FROM teams WHERE sport_id IN (1, 2, 3, 5) "
                                           "AND country IS NOT NULL")).all())

    def _map(self, etype: str, key: str, name: str, entity_id: int, method: str = "provider") -> int:
        self.s.add(SourceEntity(source_id=SOURCE, entity_type=etype, external_id=key, external_name=name[:255],
                                entity_id=entity_id, method=method))
        self.own[(etype, key)] = entity_id
        return entity_id

    def league(self, code: str) -> int:
        if ("league", code) in self.own:
            return self.own[("league", code)]
        sport, country, name = SERIES[code]
        lid = self.s.scalar(select(League.id).where(League.sport_id == SPORT_ID[sport], League.country == country,
                                                    League.name == name, League.gender == "m",
                                                    League.age_group.is_(None)))
        if not lid:  # an Olimpbet tournament of that English name ('UEFA Champions League. League phase')
            lid = self.s.scalar(select(SourceEntity.entity_id).join(League, League.id == SourceEntity.entity_id)
                                .where(SourceEntity.source_id == 3, SourceEntity.entity_type == "league",
                                       SourceEntity.external_name.ilike(f"{name}%"),
                                       League.sport_id == SPORT_ID[sport], League.gender == "m")
                                .group_by(SourceEntity.entity_id).order_by(text("count(*) DESC")).limit(1))
        if not lid:
            lg = League(sport_id=SPORT_ID[sport], country=country, name=name,
                        kind="international" if country is None else "league")
            self.s.add(lg)
            self.s.flush()
            lid = lg.id
        return self._map("league", code, name, lid, "name")

    def known(self, sport_id: int, names: list[str | None], exclude: int | None = None,
              country: str | None = None, gender: str = "m") -> int | None:
        """The one known team under any of the names; among several, the one of the league's country, then the one
        football-data knows."""
        for n in names:
            ids = (self.names.get((sport_id, gender, name_key(sport_id, n))) or set()) - {exclude} if n else set()
            if len(ids) > 1 and country:
                ids = {i for i in ids if self.country.get(i) == country} or ids
            if len(ids) > 1:
                ids = ids & self.fd_teams or ids
            if len(ids) == 1:
                return next(iter(ids))
        return None

    def team(self, sport_id: int, key: str, name: str, alias: str | None = None, country: str | None = None,
             gender: str = "m") -> int:
        if ("team", key) in self.own:
            return self.own[("team", key)]
        tid = self.known(sport_id, [name, alias], country=country, gender=gender)
        if tid:
            return self._map("team", key, name, tid, "name")
        t = Team(sport_id=sport_id, name=name[:160], kind="player" if sport_id == 2 else "club", gender=gender)
        self.s.add(t)
        self.s.flush()
        self.names.setdefault((sport_id, gender, name_key(sport_id, name)), set()).add(t.id)
        return self._map("team", key, name, t.id)

    def match(self, e: dict, sport_id: int, league_id: int, home: int, away: int, start: dt.datetime) -> int:
        key = str(e["id"])
        if ("match", key) in self.own:
            mid = self.own[("match", key)]
            self.s.execute(text("UPDATE matches SET start_time = :st WHERE id = :id AND status = 'scheduled'"),
                           {"st": start, "id": mid})
            return mid
        mid = self.s.scalar(text(
            "SELECT id FROM matches WHERE sport_id = :sp AND home_team_id = :h AND away_team_id = :a "
            "AND start_time BETWEEN :lo AND :hi LIMIT 1"),
            {"sp": sport_id, "h": home, "a": away, "lo": start - dt.timedelta(hours=36),
             "hi": start + dt.timedelta(hours=36)})
        if mid:
            return self._map("match", key, e["title"], mid, "linked")
        m = Match(sport_id=sport_id, league_id=league_id, start_time=start, home_team_id=home, away_team_id=away,
                  status="scheduled")
        self.s.add(m)
        self.s.flush()
        return self._map("match", key, e["title"], m.id)


def sides(e: dict, sport_id: int) -> list[dict]:
    """[home, away] as {key, name, alias}: event teams (ordering home / away) or the moneyline outcomes."""
    teams = e.get("teams") or []
    if len(teams) == 2:
        teams = sorted(teams, key=lambda t: 0 if t.get("ordering") == "home" else 1)
        return [{"key": str(t["id"]), "name": t["name"], "alias": t.get("alias")} for t in teams]
    ml = next((m for m in e["markets"] if m.get("sportsMarketType") == "moneyline"), None)
    if ml:
        return [{"key": f"name|{n}", "name": n, "alias": None} for n in json.loads(ml["outcomes"])[:2]]
    # old football events without teams: 'Ipswich vs. Nottingham Forest' (home first)
    names = [x.strip() for x in re.split(r"\s+vs\.?\s+", e["title"], maxsplit=1)]
    if len(names) != 2:
        raise ValueError(f"no sides in {e['title']!r}")
    return [{"key": f"name|{n}", "name": n, "alias": None} for n in names]


def side_of(name: str, home: dict, away: dict) -> str | None:
    n = norm_team(name)
    for side, t in (("home", home), ("away", away)):
        if n in {norm_team(t["name"]), norm_team(t.get("alias") or "")}:
            return side
    return None


OLD_1X2 = re.compile(r"^Will .+ (win on \d{4}-\d{2}-\d{2}|end in a draw)\?$")
GENERIC_TAGS = {"1", "100639", "100350", "864"}


def old_1x2_leg(m: dict) -> bool:
    """A leg of the football 1x2 of the 2024/25 format: untyped 'Will X win on DATE?' / 'Will X vs. Y end in a draw?'."""
    return not m.get("sportsMarketType") and bool(OLD_1X2.match(m.get("question") or ""))


def league_tags(http: HttpClient) -> dict[str, list[str]]:
    """Series code -> its league tag ids (the /sports tags without the generic ones), for events outside series."""
    return {x["sport"]: [t.strip() for t in str(x.get("tags") or "").split(",") if t.strip() not in GENERIC_TAGS]
            for x in http.get_json(f"{GAMMA}/sports") if x.get("sport") in SERIES}


def tag_event_weeks(http: HttpClient, tag: str, now: dt.datetime, days: int, football: bool):
    """Closed main game events carrying a league tag, week by week (the seasons before the current series)."""
    seen = set()
    lo_game = now - dt.timedelta(days=days)
    lo = lo_game - dt.timedelta(days=45)
    while lo < now:
        hi = lo + dt.timedelta(days=7)
        week, offset = [], 0
        while True:
            page = http.get_json(f"{GAMMA}/events", {
                "tag_id": tag, "closed": "true", "limit": 100, "offset": offset,
                "start_date_min": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "start_date_max": hi.strftime("%Y-%m-%dT%H:%M:%SZ")})
            for e in page:
                if e["id"] in seen or e.get("parentEventId") or " vs" not in (e.get("title") or ""):
                    continue
                seen.add(e["id"])
                try:
                    if not (lo_game <= kickoff(e) <= now):
                        continue
                except (KeyError, ValueError):
                    continue
                e["markets"] = [m for m in e.get("markets") or []
                                if m.get("sportsMarketType") in KINDS or (football and old_1x2_leg(m))]
                if any(m.get("sportsMarketType") == "moneyline" or old_1x2_leg(m) for m in e["markets"]):
                    week.append(e)
            if len(page) < 100 or offset >= 1900:
                break
            offset += 100
        yield week
        lo = hi


def set_number(m: dict) -> int | None:
    found = re.search(r"Set (\d)", f"{m.get('question') or ''} {m.get('groupItemTitle') or ''}")
    return int(found[1]) if found else None


def markets_of(e: dict, home: dict, away: dict, football: bool, closed: bool = False) -> list[dict]:
    """Market rows {kind, period, line, subject, tokens {outcome: token}, conditions [...]} of one event; closed
    markets only with closed=True (price history of past games). Shapes: see KINDS."""
    out = []
    markets = [m for m in e["markets"] if m.get("clobTokenIds") and m.get("outcomes")]  # never listed: no tokens
    ml = [m for m in markets if (m.get("sportsMarketType") == "moneyline" or (football and old_1x2_leg(m)))
          and (closed or not m.get("closed"))]
    if football and len(ml) == 3:
        tokens = {}
        for m in ml:
            yes = json.loads(m["clobTokenIds"])[json.loads(m["outcomes"]).index("Yes")]
            title = m.get("groupItemTitle") or ""
            side = "draw" if title.startswith("Draw") else side_of(title, home, away)
            if side:
                tokens[side] = yes
        if len(tokens) == 3:
            out.append({"kind": "1x2", "period": 0, "line": None, "subject": None, "tokens": tokens,
                        "conditions": [m["conditionId"] for m in ml]})
    for m in markets:
        t = m.get("sportsMarketType")
        if (m.get("closed") and not closed) or t not in KINDS or (t == "moneyline" and football):
            continue
        kind, period, shape = KINDS[t]
        if period is None:  # 'Set 2 Winner' / 'Set 2 Games O/U'
            period = int(m["line"]) if t == "tennis_set_winner" and m.get("line") else set_number(m)
            if period is None:
                continue
        outs, toks = json.loads(m["outcomes"]), json.loads(m["clobTokenIds"])
        if len(outs) != 2 or len(toks) != 2:
            continue
        row = {"kind": kind, "period": period, "line": None, "subject": None, "conditions": [m["conditionId"]]}
        if shape in ("ou", "team_ou"):
            names = ["over" if o.lower().startswith("over") else "under" for o in outs]
            if set(names) != {"over", "under"}:
                continue
            row.update(line=m.get("line"), tokens=dict(zip(names, toks)))
            if shape == "team_ou":  # 'Kings Team Total: O/U 106.5' / 'Kings 1H O/U 54.5'
                team = re.split(r"\s+(Team Total|1H|2H|O/U)", m.get("groupItemTitle") or m.get("question") or "")[0]
                row["subject"] = side_of(team, home, away)
                if not row["subject"]:
                    continue
        elif shape in ("prop", "yesno"):
            names = ["yes" if o.lower() == "yes" else "no" for o in outs]
            if set(names) != {"yes", "no"}:
                continue
            row["tokens"] = dict(zip(names, toks))
            if shape == "prop":  # 'Victor Wembanyama: Points O/U 28.5' - yes = over the line
                row.update(line=m.get("line"), subject=(m.get("question") or "").split(":")[0].strip()[:80] or None)
                if not row["subject"]:
                    continue
        else:  # sides / sides_line
            s0 = side_of(outs[0], home, away) or ("home" if t == "moneyline" else None)
            if not s0:
                continue
            s1 = "away" if s0 == "home" else "home"
            row["tokens"] = {s0: toks[0], s1: toks[1]}
            if shape == "sides_line" and m.get("line") is not None:
                row["line"] = m["line"] if s0 == "home" else -m["line"]  # stored as the home handicap
        out.append(row)
    return out


def upsert_market(s, match_id: int, mk: dict, event_id) -> int:
    """The market row of a match (Polymarket tokens / conditions in extra, merged into a row another source made)."""
    line = Decimal(str(mk["line"])) if mk["line"] is not None else None
    extra = {"tokens": mk["tokens"], "conditions": mk["conditions"], "event": str(event_id)}
    stmt = pg_insert(Market).values(match_id=match_id, kind=mk["kind"], period=mk.get("period") or 0, line=line,
                                    subject=mk.get("subject"), extra=extra)
    return s.scalar(stmt.on_conflict_do_update(
        constraint="markets_match_kind_period_line_subject_key",
        set_={"extra": text("COALESCE(markets.extra, '{}'::jsonb) || excluded.extra")}).returning(Market.id))


def discover(http: HttpClient, now: dt.datetime) -> tuple[int, int]:
    ids = series_ids(http)
    n_events = n_markets = 0
    for code, series in ids.items():
        sport = SERIES[code][0]
        sport_id = SPORT_ID[sport]
        events = [e for e in open_events(http, series, now) if not e.get("parentEventId")
                  and any(m.get("sportsMarketType") == "moneyline" for m in e.get("markets") or [])]
        with session() as s:
            r = Resolver(s)
            league_id = r.league(code)
            for e in events:
                start = kickoff(e)
                home, away = sides(e, sport_id)
                country = SERIES[code][1]
                hid = r.team(sport_id, home["key"], home["name"], home.get("alias"), country, GENDER.get(code, "m"))
                aid = r.team(sport_id, away["key"], away["name"], away.get("alias"), country, GENDER.get(code, "m"))
                mid = r.match(e, sport_id, league_id, hid, aid, start)
                for mk in markets_of(e, home, away, sport == "football"):
                    upsert_market(s, mid, mk, e["id"])
                    n_markets += 1
            n_events += len(events)
    return n_events, n_markets


def closed_event_weeks(http: HttpClient, series: str, now: dt.datetime, days: int):
    """Closed main events of a series whose game was within the last `days`, yielded week by week of startDate
    (Gamma refuses large offsets); markets trimmed to the kinds we keep (an NBA event has ~500 player markets)."""
    seen = set()
    lo_game = now - dt.timedelta(days=days)
    lo = lo_game - dt.timedelta(days=45)  # markets open up to a month before the game (NHL)
    while lo < now:
        hi = lo + dt.timedelta(days=7)
        week, offset = [], 0
        while True:
            page = http.get_json(f"{GAMMA}/events", {
                "series_id": series, "closed": "true", "limit": 100, "offset": offset,
                "start_date_min": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "start_date_max": hi.strftime("%Y-%m-%dT%H:%M:%SZ")})
            for e in page:
                if e["id"] in seen or e.get("parentEventId") or not (lo_game <= kickoff(e) <= now):
                    continue
                seen.add(e["id"])
                e["markets"] = [m for m in e.get("markets") or [] if m.get("sportsMarketType") in KINDS]
                if any(m.get("sportsMarketType") == "moneyline" for m in e["markets"]):
                    week.append(e)
            if len(page) < 100 or offset >= 1900:
                break
            offset += 100
        yield week
        lo = hi


def closed_events(http: HttpClient, series: str, now: dt.datetime, days: int) -> list[dict]:
    return [e for week in closed_event_weeks(http, series, now, days) for e in week]


def history(http: HttpClient, now: dt.datetime, days: int) -> int:
    """Teams and matches (no prices) of the closed events of the last `days`: the past games let
    normalization.polymarket_links map Polymarket teams to ours by vote before the next games."""
    n = 0
    for code, series in series_ids(http).items():
        sport_id = SPORT_ID[SERIES[code][0]]
        events = [e for e in closed_events(http, series, now, days) if not e.get("parentEventId")
                  and any(m.get("sportsMarketType") == "moneyline" for m in e.get("markets") or [])]
        with session() as s:
            r = Resolver(s)
            league_id = r.league(code)
            for e in events:
                home, away = sides(e, sport_id)
                hid = r.team(sport_id, home["key"], home["name"], home.get("alias"), SERIES[code][1],
                             GENDER.get(code, "m"))
                aid = r.team(sport_id, away["key"], away["name"], away.get("alias"), SERIES[code][1],
                             GENDER.get(code, "m"))
                r.match(e, sport_id, league_id, hid, aid, kickoff(e))
        print(f"  {code}: {len(events)} closed events", flush=True)
        n += len(events)
    return n


def books(tokens: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(tokens), 100):
        r = httpx.post(f"{CLOB}/books", json=[{"token_id": t} for t in tokens[i:i + 100]], timeout=30)
        r.raise_for_status()
        for b in r.json():
            bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids") or []]
            asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks") or []]
            bid = max(bids, default=None)
            ask = min(asks, default=None)
            out[b["asset_id"]] = {"bid": bid[0] if bid else None, "ask": ask[0] if ask else None,
                                  "ask_size": ask[1] if ask else None}
        time.sleep(0.2)
    return out


EXTRA_HOURS = 6        # props / halves / quarters: order books only in the last hours before the start
SIZE_MOVE = 0.30       # a new row when bid / ask changed, or the size at the best ask moved by 30%+


def snapshot(now: dt.datetime) -> int:
    with session() as s:
        rows = s.execute(text("""
            SELECT mk.id, o.key, o.value FROM markets mk JOIN matches m ON m.id = mk.match_id,
                   jsonb_each_text(mk.extra->'tokens') o
            WHERE mk.extra ? 'tokens' AND m.status IN ('scheduled', 'live')
              AND m.start_time BETWEEN :lo AND :hi
              AND ((mk.period = 0 AND mk.subject IS NULL) OR m.start_time <= :extra)"""),
                         {"lo": now - dt.timedelta(hours=LIVE_HOURS), "hi": now + dt.timedelta(days=SNAPSHOT_DAYS),
                          "extra": now + dt.timedelta(hours=EXTRA_HOURS)}).all()
        last = {(mid, out): (b, a, z) for mid, out, b, a, z in s.execute(text("""
            SELECT DISTINCT ON (market_id, outcome) market_id, outcome, bid, ask, ask_size FROM odds_history
            WHERE source_id = :src AND snapshot IS NULL AND market_id = ANY(:ids)
            ORDER BY market_id, outcome, taken_at DESC"""),
            {"src": SOURCE, "ids": list({r[0] for r in rows})})}
    got = books([r[2] for r in rows])
    quotes = []
    for market_id, outcome, token in rows:
        b = got.get(token)
        if not b or (b["bid"] is None and b["ask"] is None):
            continue
        prev = last.get((market_id, outcome))
        if prev:
            pb, pa, pz = (float(x) if x is not None else None for x in prev)
            same_price = pb == b["bid"] and pa == b["ask"]
            size_moved = (pz is None) != (b["ask_size"] is None) or (
                pz and b["ask_size"] is not None and abs(b["ask_size"] - pz) / pz > SIZE_MOVE)
            if same_price and not size_moved:
                continue
        mid_price = (b["bid"] + b["ask"]) / 2 if b["bid"] is not None and b["ask"] is not None else None
        quotes.append({"market_id": market_id, "outcome": outcome, "source_id": SOURCE, "bookmaker": "polymarket",
                       "bid": b["bid"], "ask": b["ask"], "ask_size": b["ask_size"],
                       "price": round(mid_price, 5) if mid_price is not None else None,
                       "odds": round(1 / b["ask"], 3) if b["ask"] else None, "taken_at": now})
    if quotes:
        with session() as s:
            for c in range(0, len(quotes), 5000):
                s.execute(pg_insert(OddsQuote), quotes[c:c + 5000])
    return len(quotes)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", type=int, metavar="DAYS", help="only add the closed events of the last DAYS")
    args = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    http = HttpClient(min_interval=0.25)
    t0 = time.time()
    if args.history:
        print(f"polymarket history: {history(http, now, args.history)} closed events in {time.time() - t0:.0f}s")
        return
    events, markets = discover(http, now)
    quotes = snapshot(now)
    print(f"polymarket: {events} events, {markets} markets upserted, {quotes} changed quotes stored "
          f"in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
