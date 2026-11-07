"""football-data.co.uk -> PostgreSQL directly (the regular collection; data_engine/legacy/football_data.py was the
one-time import from the local sqlite): the current season files of the 22 main leagues and the 16 extra
countries' all-seasons files, which the site updates twice a week. New rows only (key div|date|home|away):

- a match already in the database (Polymarket / Olimpbet / the archive: same teams, +-1 day) is linked and gets
  the result if it has none, else a new match is created - so the same game is never stored twice;
- statistics (shots, corners, cards ...) and the markets 1x2 / total 2.5 / Asian handicap with pre-match and
  closing odds (Pinnacle, market average / max, Bet365, Betfair Exchange) as in the legacy import; a market the
  match already has (Polymarket's 1x2) gets the bookmaker quotes added.

  python -u -m data_engine.providers.football_data [--seasons 2]   # current season + the previous one
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from decimal import Decimal

from sqlalchemy import delete, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db import session
from core.models import Market, Match, MatchStatistic, OddsQuote, RawPayload, SourceEntity
from data_engine.legacy.football_data import (BROKEN_FILES, OU25, SOURCE, SPORT, STATS, Refs, kickoff, quotes_1x2,
                                              quotes_ah, quotes_two_way, season_name)
from pmbot import footballdata as fd
from pmbot.http import HttpClient

BASE = "https://football-data.co.uk"  # www. redirects here; conditional requests by the ETag of each file


def fetch(http: HttpClient, seasons: int) -> tuple[list[dict], dict[str, str]]:
    """Rows of the files changed since the last run: conditional requests by the ETag kept in raw_payloads
    (kind 'fd_etag'), so an hourly run downloads nothing when the site did not update (304)."""
    first = fd.current_season() - seasons + 1
    with session() as s:
        etags = dict(s.execute(select(RawPayload.external_id, RawPayload.payload["etag"].astext).where(
            RawPayload.source_id == SOURCE, RawPayload.kind == "fd_etag")).all())
    out, changed = [], {}
    for path, div, season in fd.files(first, extra=True):
        r = http.get(f"{BASE}/{path}", headers={"If-None-Match": etags[path]} if etags.get(path) else None)
        if r.status_code == 304:
            continue
        if r.status_code == 404 or not r.content.lstrip(b"\xef\xbb\xbf").startswith((b"Div", b"Country")):
            continue  # league not covered that season
        r.raise_for_status()
        if r.headers.get("etag"):
            changed[path] = r.headers["etag"]
        rows = [{f: rec.get(f) for f in fd.FIELDS} for rec in fd.parse_csv(r.content, div, season)]
        out += [x for x in rows if x["season"] is not None and x["season"] >= first
                and (x["div"], x["season"]) not in BROKEN_FILES]
    return out, changed


def save_etags(changed: dict[str, str]) -> None:
    with session() as s:
        for path, etag in changed.items():
            s.execute(delete(RawPayload).where(RawPayload.source_id == SOURCE, RawPayload.kind == "fd_etag",
                                               RawPayload.external_id == path))
            s.execute(insert(RawPayload).values(source_id=SOURCE, kind="fd_etag", external_id=path,
                                                payload={"etag": etag}))


def key(r: dict) -> str:
    return f"{r['div']}|{r['date']}|{r['home']}|{r['away']}"


def import_rows(s, refs: Refs, rows: list[dict]) -> dict:
    """New rows of one league season."""
    div, season = rows[0]["div"], rows[0]["season"]
    league_id, country = refs.league(div, rows[0]["league"])
    season_id = refs.season(league_id, season_name(div, season))
    starts = [kickoff(r["date"], r["time"]) for r in rows]
    lo = min(st for st, _ in starts) - dt.timedelta(days=2)
    hi = max(st for st, _ in starts) + dt.timedelta(days=2)
    index = {(h, a, st.astimezone(dt.timezone.utc).date()): (mid, status) for mid, h, a, st, status in s.execute(
        select(Match.id, Match.home_team_id, Match.away_team_id, Match.start_time, Match.status)
        .where(Match.sport_id == SPORT, Match.start_time.between(lo, hi)))}
    cnt = {"new": 0, "linked": 0, "odds": 0}
    for r, (start, known) in zip(rows, starts):
        home, away = refs.team(country, r["home"]), refs.team(country, r["away"])
        periods = None
        if r["hthg"] is not None and r["htag"] is not None:
            periods = [[r["hthg"], r["htag"]], [r["fthg"] - r["hthg"], r["ftag"] - r["htag"]]]
        hit = next((index[k] for d in (0, -1, 1)
                    if (k := (home, away, start.date() + dt.timedelta(days=d))) in index), None)
        if hit:
            mid = hit[0]
            if hit[1] != "finished":
                s.execute(text("UPDATE matches SET status = 'finished', home_score = :h, away_score = :a, "
                               "periods = CAST(:p AS jsonb), season_id = COALESCE(season_id, :se) WHERE id = :id"),
                          {"h": r["fthg"], "a": r["ftag"], "p": json.dumps(periods) if periods else None,
                           "se": season_id, "id": mid})
            cnt["linked"] += 1
        else:
            mid = s.scalar(insert(Match).values(
                sport_id=SPORT, league_id=league_id, season_id=season_id, start_time=start, time_known=known,
                home_team_id=home, away_team_id=away, status="finished", home_score=r["fthg"],
                away_score=r["ftag"], periods=periods).returning(Match.id))
            index[(home, away, start.date())] = (mid, "finished")
            cnt["new"] += 1
        s.add(SourceEntity(source_id=SOURCE, entity_type="match", external_id=key(r),
                           external_name=f"{r['home']} - {r['away']}", entity_id=mid,
                           method="linked" if hit else "provider"))
        stats = [{"match_id": mid, "source_id": SOURCE, "stat": stat, "period": 0,
                  "home_value": Decimal(str(r[hc])), "away_value": Decimal(str(r[ac]))}
                 for stat, hc, ac in STATS if r[hc] is not None and r[ac] is not None]
        if stats:
            s.execute(pg_insert(MatchStatistic).on_conflict_do_nothing(), stats)
        odds = json.loads(r["odds"] or "{}")
        markets = [("1x2", None, quotes_1x2(odds)), ("total", Decimal("2.5"), quotes_two_way(odds, OU25))]
        markets += [("spread", Decimal(str(line)), q) for line, q in quotes_ah(odds).items()]
        for kind, line, quotes in markets:
            if not quotes:
                continue
            market_id = s.scalar(pg_insert(Market).values(match_id=mid, kind=kind, period=0, line=line)
                                 .on_conflict_do_update(constraint="markets_match_id_kind_period_line_key",
                                                        set_={"kind": kind}).returning(Market.id))
            s.execute(insert(OddsQuote), [{"market_id": market_id, "outcome": o, "source_id": SOURCE,
                                           "bookmaker": b, "snapshot": snap, "odds": Decimal(str(v))}
                                          for b, snap, o, v in quotes])
            cnt["odds"] += len(quotes)
    return cnt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, default=2, help="seasons back to look at (rows already imported skip)")
    args = ap.parse_args()
    t0 = time.time()
    http = HttpClient(min_interval=1.0, retries=4, timeout=60, headers={"Accept": "text/csv,*/*"})
    rows, changed = fetch(http, args.seasons)
    with session() as s:
        existing = set(s.scalars(select(SourceEntity.external_id).where(
            SourceEntity.source_id == SOURCE, SourceEntity.entity_type == "match")))
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        if key(r) not in existing:
            groups.setdefault((r["div"], r["season"]), []).append(r)
    total = {"new": 0, "linked": 0, "odds": 0}
    for (div, season), part in sorted(groups.items(), key=lambda g: (g[0][1], g[0][0])):
        with session() as s:
            c = import_rows(s, Refs(s), sorted(part, key=lambda r: r["date"]))
        total = {k: total[k] + c[k] for k in total}
        print(f"  {div:<4} {season}: +{c['new']} new, {c['linked']} linked, {c['odds']} odds", flush=True)
    save_etags(changed)  # after the import: a failed run fetches the files again
    print(f"football-data: {len(changed)} files changed, {len(rows)} rows in them, "
          f"{sum(map(len, groups.values()))} new: {total} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
