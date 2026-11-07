"""Duplicate teams / matches of different sources -> one row.

- merge_team(src, dst): every match, mapping and statistic of team src goes to dst, src is deleted (a renamed
  franchise: 'Utah Hockey Club' -> 'Utah Mammoth'; a club two sources created twice);
- duplicate matches: same sport, same home and away teams, start within 36 h, same score (or one without a
  result); rows of one source only when the start time and the score are identical too (amateur leagues: the same
  teams meet again the next day). The kept row: the one with Polymarket markets, else the most source mappings,
  else the oldest; mappings, statistics, events, markets / odds and a missing result move into it.

  python -m data_engine.normalization.dedupe [--sport 5] [--dry]
  python -m data_engine.normalization.dedupe --merge-team 163194 110097
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from core.db import session
from data_engine.normalization.polymarket_links import move_markets

PM = 4


def merge_team(s, src: int, dst: int) -> None:
    for side in ("home_team_id", "away_team_id"):
        s.execute(text(f"UPDATE matches SET {side} = :d WHERE {side} = :s"), {"s": src, "d": dst})
    s.execute(text("UPDATE source_entities SET entity_id = :d WHERE entity_type = 'team' AND entity_id = :s"),
              {"s": src, "d": dst})
    s.execute(text("DELETE FROM teams WHERE id = :s"), {"s": src})


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
    ap.add_argument("--sport", type=int, action="append", help="sport id (repeatable; default 1, 2, 3, 5)")
    ap.add_argument("--merge-team", type=int, nargs=2, metavar=("SRC", "DST"))
    ap.add_argument("--since", default="2024-06-01", help="matches from this date (the history horizon)")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    with session() as s:
        if args.merge_team:
            merge_team(s, *args.merge_team)
            print(f"team {args.merge_team[0]} -> {args.merge_team[1]}")
        for sport in args.sport or [1, 2, 3, 5]:
            found = pairs(s, sport, args.since)
            print(f"sport {sport}: {len(found)} duplicate matches", flush=True)
            if not args.dry:
                for keep, drop in found:
                    merge_match(s, keep, drop)
        if args.dry:
            s.rollback()


if __name__ == "__main__":
    main()
