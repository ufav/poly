"""Duplicate teams / matches of different sources -> one row.

- merge_team(src, dst): every match, mapping and statistic of team src goes to dst, src is deleted (a renamed
  franchise: 'Utah Hockey Club' -> 'Utah Mammoth'; a club two sources created twice);
- duplicate matches: same sport, same home and away teams, start within 36 h, same score (or one without a
  result); rows of one source only when the start time and the score are identical too (amateur leagues: the same
  teams meet again the next day). The kept row: the one with Polymarket markets, else the most source mappings,
  else the oldest; mappings, statistics, events, markets / odds and a missing result move into it.

  python -m data_engine.normalization.dedupe [--sport 5] [--dry]
  python -m data_engine.normalization.dedupe --merge-team 163194 110097
  python -m data_engine.normalization.dedupe --vote-source 9 --sport 1   # ESPN-made clubs -> the known ones
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from core.db import session
from collections import Counter, defaultdict

from data_engine.normalization.polymarket_links import move_markets, similar, words

PM = 4


def merge_team(s, src: int, dst: int) -> None:
    for side in ("home_team_id", "away_team_id"):
        s.execute(text(f"UPDATE matches SET {side} = :d WHERE {side} = :s"), {"s": src, "d": dst})
    s.execute(text("UPDATE source_entities SET entity_id = :d WHERE entity_type = 'team' AND entity_id = :s"),
              {"s": src, "d": dst})
    s.execute(text("DELETE FROM teams WHERE id = :s"), {"s": src})


def vote_teams(s, source: int, sport: int, since: str) -> int:
    """Teams only `source` knows (it created them; Polymarket may have matched them by name) -> the team of another
    source: the source's finished matches vs
    the other finished matches of the sport within 36 h with the same score and similar names of both sides (same
    gender / age); a team merges when its votes agree (one vote with close names, else two)."""
    own_only = {r[0] for r in s.execute(text("""
        SELECT t.id FROM teams t WHERE t.sport_id = :sp AND EXISTS (SELECT 1 FROM source_entities se
          WHERE se.entity_type = 'team' AND se.entity_id = t.id AND se.source_id = :src)
          AND NOT EXISTS (SELECT 1 FROM source_entities se WHERE se.entity_type = 'team' AND se.entity_id = t.id
                          AND se.source_id NOT IN (:src, :pm))"""), {"sp": sport, "src": source, "pm": PM})}
    if not own_only:
        return 0
    rows = s.execute(text("""
        SELECT x.id, x.home_team_id, x.away_team_id, y.home_team_id, y.away_team_id
        FROM matches x JOIN matches y ON y.sport_id = x.sport_id AND y.id <> x.id
         AND y.start_time BETWEEN x.start_time - interval '36 hours' AND x.start_time + interval '36 hours'
         AND y.home_score = x.home_score AND y.away_score = x.away_score
        WHERE x.sport_id = :sp AND x.start_time >= :since AND x.status = 'finished' AND y.status = 'finished'
          AND (x.home_team_id = ANY(:own) OR x.away_team_id = ANY(:own))
          AND NOT (y.home_team_id = ANY(:own) OR y.away_team_id = ANY(:own))"""),
                     {"sp": sport, "since": since, "own": list(own_only)}).all()
    ids = {t for r in rows for t in r[1:]}
    names, meta = defaultdict(set), {}
    for tid, n in s.execute(text("""SELECT id, name FROM teams WHERE id = ANY(:ids) UNION SELECT entity_id,
            external_name FROM source_entities WHERE entity_type = 'team' AND entity_id = ANY(:ids)
            AND external_name IS NOT NULL"""), {"ids": list(ids)}):
        names[tid].add(words(n))
    for tid, g, a in s.execute(text("SELECT id, gender, age_group FROM teams WHERE id = ANY(:ids)"), {"ids": list(ids)}):
        meta[tid] = (g, a)
    by_match = defaultdict(list)
    for mid, xh, xa, yh, ya in rows:
        if (meta.get(xh), meta.get(xa)) != (meta.get(yh), meta.get(ya)):
            continue
        sh = 3 if xh == yh else similar(names[xh], names[yh])
        sa = 3 if xa == ya else similar(names[xa], names[ya])
        if sh and sa:
            by_match[mid].append((sh + sa, (xh, yh, sh), (xa, ya, sa)))
    votes, strong = defaultdict(Counter), defaultdict(set)
    for cands in by_match.values():
        cands.sort(key=lambda c: -c[0])
        if len(cands) > 1 and cands[0][0] == cands[1][0]:
            continue
        for x, y, sc in cands[0][1:]:
            if x in own_only and x != y:
                votes[x][y] += 1
                if sc >= 2:
                    strong[x].add(y)
    merged = 0
    for tid, c in votes.items():
        (target, n), *rest = c.most_common()
        if target in own_only or (rest and rest[0][1] * 2 > n) or (target not in strong[tid] and n < 2):
            continue
        a, b = s.execute(text("SELECT name FROM teams WHERE id IN (:a, :b) ORDER BY id = :a DESC"),
                         {"a": tid, "b": target}).scalars().all()
        print(f"  team {a!r} -> {b!r} ({n} vote{'s' * (n > 1)})")
        merge_team(s, tid, target)
        merged += 1
    return merged


def merge_league(s, src: int, dst: int) -> None:
    """League src (a source's own copy of a competition: ESPN's 'UEFA Champions League') -> dst; its matches and
    mappings move, seasons too unless dst has one of that name."""
    s.execute(text("UPDATE matches SET league_id = :d WHERE league_id = :s"), {"s": src, "d": dst})
    s.execute(text("UPDATE source_entities SET entity_id = :d WHERE entity_type = 'league' AND entity_id = :s"),
              {"s": src, "d": dst})
    s.execute(text("""UPDATE seasons x SET league_id = :d WHERE x.league_id = :s AND NOT EXISTS (
        SELECT 1 FROM seasons y WHERE y.league_id = :d AND y.name = x.name)"""), {"s": src, "d": dst})
    s.execute(text("""UPDATE matches m SET season_id = y.id FROM seasons x, seasons y WHERE m.season_id = x.id
        AND x.league_id = :s AND y.league_id = :d AND y.name = x.name"""), {"s": src, "d": dst})
    s.execute(text("DELETE FROM seasons WHERE league_id = :s"), {"s": src})
    s.execute(text("DELETE FROM leagues WHERE id = :s"), {"s": src})


def twin_teams(s, sport: int, since: str, min_games: int = 3, dry: bool = False) -> int:
    """One club stored as two teams by two sources ('Newcastle' / 'Newcastle United'): both play the same opponent
    on the same side within 36 h with the same score, at least `min_games` times, never against each other, and the
    names are similar. The kept team: the one more sources know (football-data first), then the one with more games."""
    rows = s.execute(text("""
        WITH m AS (SELECT id, home_team_id h, away_team_id a, start_time t, home_score hs, away_score aws
                   FROM matches WHERE sport_id = :sp AND status = 'finished' AND start_time >= :since)
        SELECT x.h, y.h, count(*) FROM m x JOIN m y ON x.a = y.a AND x.h <> y.h AND x.hs = y.hs AND x.aws = y.aws
          AND y.t BETWEEN x.t - interval '36 hours' AND x.t + interval '36 hours' AND x.h < y.h
        GROUP BY 1, 2 HAVING count(*) >= :n
        UNION ALL
        SELECT x.a, y.a, count(*) FROM m x JOIN m y ON x.h = y.h AND x.a <> y.a AND x.hs = y.hs AND x.aws = y.aws
          AND y.t BETWEEN x.t - interval '36 hours' AND x.t + interval '36 hours' AND x.a < y.a
        GROUP BY 1, 2 HAVING count(*) >= :n"""), {"sp": sport, "since": since, "n": min_games}).all()
    votes = Counter()
    for a, b, n in rows:
        votes[(a, b)] += n
    if not votes:
        return 0
    ids = list({t for pair in votes for t in pair})
    names, meta = defaultdict(set), {}
    for tid, n in s.execute(text("""SELECT id, name FROM teams WHERE id = ANY(:ids) UNION SELECT entity_id,
            external_name FROM source_entities WHERE entity_type = 'team' AND entity_id = ANY(:ids)
            AND external_name IS NOT NULL"""), {"ids": ids}):
        names[tid].add(words(n))
    for tid, g, a, name in s.execute(text("SELECT id, gender, age_group, name FROM teams WHERE id = ANY(:ids)"),
                                     {"ids": ids}):
        meta[tid] = (g, a, name)
    rank = {tid: (fd, n_src, n_games) for tid, fd, n_src, n_games in s.execute(text("""
        SELECT t.id, bool_or(se.source_id = 1), count(DISTINCT se.source_id),
               (SELECT count(*) FROM matches m WHERE m.home_team_id = t.id) + (SELECT count(*) FROM matches m
                WHERE m.away_team_id = t.id)
        FROM teams t LEFT JOIN source_entities se ON se.entity_type = 'team' AND se.entity_id = t.id
        WHERE t.id = ANY(:ids) GROUP BY t.id"""), {"ids": ids})}
    met = {(min(a, b), max(a, b)) for a, b in s.execute(text("""SELECT home_team_id, away_team_id FROM matches
        WHERE (home_team_id = ANY(:ids) AND away_team_id = ANY(:ids))"""), {"ids": ids})}
    merged, gone = 0, set()
    for (a, b), n in votes.most_common():
        # 10+ such games are the same club whatever the names ("Borussia Monchengladbach" / "M'gladbach")
        if a in gone or b in gone or (a, b) in met or meta[a][:2] != meta[b][:2]                 or (n < 10 and not similar(names[a], names[b])):
            continue
        keep, drop = (a, b) if (rank[a][0] or False, rank[a][1], rank[a][2]) >= (rank[b][0] or False, rank[b][1],
                                                                               rank[b][2]) else (b, a)
        print(f"  twin {meta[drop][2]!r} -> {meta[keep][2]!r} ({n} same games)")
        if not dry:
            merge_team(s, drop, keep)
        gone.add(drop)
        merged += 1
    return merged


def pairs(s, sport: int, since: str) -> list[tuple[int, int]]:
    """(keep, drop) duplicate match pairs."""
    rows = s.execute(text("""
        WITH m AS (
          SELECT m.id, m.home_team_id h, m.away_team_id a, m.start_time t, m.home_score hs, m.away_score aws,
                 m.status, (SELECT array_agg(DISTINCT se.source_id) FROM source_entities se
                            WHERE se.entity_type = 'match' AND se.entity_id = m.id) src,
                 EXISTS (SELECT 1 FROM markets mk WHERE mk.match_id = m.id AND mk.extra ? 'tokens') pm
          FROM matches m WHERE m.sport_id = :sp AND m.start_time >= :since
            AND m.status IN ('finished', 'scheduled', 'walkover'))
        SELECT x.id, y.id, x.src, y.src, x.pm, y.pm, x.t = y.t AND x.hs IS NOT DISTINCT FROM y.hs
               AND x.aws IS NOT DISTINCT FROM y.aws
        FROM m x JOIN m y ON x.h = y.h AND x.a = y.a AND x.id < y.id
         AND y.t BETWEEN x.t - interval '36 hours' AND x.t + interval '36 hours'
        WHERE (x.hs IS NULL OR y.hs IS NULL OR (x.hs = y.hs AND x.aws = y.aws))"""),
                     {"sp": sport, "since": since}).all()
    out, used = [], set()
    for a, b, src_a, src_b, pm_a, pm_b, identical in rows:
        src_a, src_b = set(src_a or []), set(src_b or [])
        if a in used or b in used:
            continue
        if src_a & src_b and not identical:
            continue  # the same source twice: two games unless everything is identical
        keep, drop = (a, b) if (pm_a, len(src_a), -a) >= (pm_b, len(src_b), -b) else (b, a)
        out.append((keep, drop))
        used |= {a, b}
    return out


def merge_match(s, keep: int, drop: int) -> None:
    s.execute(text("""
        UPDATE matches k SET status = d.status, home_score = d.home_score, away_score = d.away_score,
               periods = COALESCE(k.periods, d.periods), result_extra = COALESCE(k.result_extra, d.result_extra)
        FROM matches d WHERE k.id = :k AND d.id = :d AND k.home_score IS NULL AND d.home_score IS NOT NULL"""),
              {"k": keep, "d": drop})
    s.execute(text("""UPDATE matches k SET periods = COALESCE(k.periods, d.periods),
               result_extra = COALESCE(k.result_extra, '{}'::jsonb) || COALESCE(d.result_extra, '{}'::jsonb),
               stage = COALESCE(k.stage, d.stage), season_id = COALESCE(k.season_id, d.season_id)
        FROM matches d WHERE k.id = :k AND d.id = :d"""), {"k": keep, "d": drop})
    s.execute(text("""INSERT INTO match_statistics (match_id, source_id, stat, period, home_value, away_value)
        SELECT :k, source_id, stat, period, home_value, away_value FROM match_statistics WHERE match_id = :d
        ON CONFLICT DO NOTHING"""), {"k": keep, "d": drop})
    s.execute(text("UPDATE match_events SET match_id = :k WHERE match_id = :d"), {"k": keep, "d": drop})
    move_markets(s, drop, keep)
    s.execute(text("UPDATE source_entities SET entity_id = :k WHERE entity_type = 'match' AND entity_id = :d"),
              {"k": keep, "d": drop})
    s.execute(text("DELETE FROM matches WHERE id = :d"), {"d": drop})


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", type=int, action="append", help="sport id (repeatable; default all five)")
    ap.add_argument("--merge-team", type=int, nargs=2, metavar=("SRC", "DST"))
    ap.add_argument("--merge-league", type=int, nargs=2, metavar=("SRC", "DST"), action="append")
    ap.add_argument("--since", default="2024-06-01", help="matches from this date (the history horizon)")
    ap.add_argument("--vote-source", type=int, help="first merge the teams only this source created, by vote")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    with session() as s:
        for src, dst in args.merge_league or []:
            merge_league(s, src, dst)
            print(f"league {src} -> {dst}")
        if args.merge_league and not args.sport:
            return
        if args.merge_team:
            merge_team(s, *args.merge_team)
            print(f"team {args.merge_team[0]} -> {args.merge_team[1]}")
        for sport in args.sport or [1, 2, 3, 4, 5]:
            print(f"sport {sport}: {twin_teams(s, sport, args.since, dry=args.dry)} twin teams merged", flush=True)
            if args.vote_source:
                print(f"sport {sport}: {vote_teams(s, args.vote_source, sport, args.since)} teams of source "
                      f"{args.vote_source} merged by vote", flush=True)
            found = pairs(s, sport, args.since)
            print(f"sport {sport}: {len(found)} duplicate matches", flush=True)
            if not args.dry:
                for keep, drop in found:
                    merge_match(s, keep, drop)
        if args.dry:
            s.rollback()


if __name__ == "__main__":
    main()
