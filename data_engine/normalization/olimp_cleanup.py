"""Clean up after the Olimp archive import (data_engine.legacy.olimp_archive):

1. leagues: Olimp puts the stage into the tournament ('Кубок лиги. 1/8 финала', 'Лига Чемпионов УЕФА. Групповой
   этап. 2-й тур'), so every stage became a league. The stage is cut off into matches.stage and leagues whose base
   names differ only by case / Cyrillic-Latin look-alikes / dashes / УЕФА-UEFA are merged (football-data leagues are
   kept as they are and absorb Olimp variants mapped to them);
2. duplicates: the same match under two tournament names (same teams, same score, start within 24 h) is merged into
   one - events, statistics, markets and source_entities move to the kept match (football-data one first, else the
   oldest id).

  python -m data_engine.normalization.olimp_cleanup [--dry-run]
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict

from sqlalchemy import text

from core.db import session

SPORT = 1
OLIMP = 2
FD = 1
STAGE = re.compile(
    r"^(\d+-[йяе] |1/\d+ финала|финал|полуфинал|четвертьфинал|групповой этап|группа\b|группы\b|\d+-й тур|тур \d+|"
    r"первые матчи|ответные матчи|квалификац|отборочн|плей-?офф|плей-?аут|чемпионская группа|"
    r"этап|раунд|\d+-й раунд|предварительн|стыков|переходн|матчи за|за \d|апертура|клаусура|основной этап|"
    r"регулярный|лига-этап|общий этап|утешительн|1-й этап|2-й этап|первый этап|второй этап|"
    r"зона|конференция|дивизион [a-zа-я]$|north|south|east|west)", re.IGNORECASE)
HOMOGLYPH = str.maketrans("АВЕКМНОРСТХаеорсух", "ABEKMHOPCTXaeopcyx")


def split_stage(name: str) -> tuple[str, str | None]:
    parts = name.split(". ")
    for i, p in enumerate(parts):
        if i > 0 and STAGE.match(p.strip()):
            return ". ".join(parts[:i]), ". ".join(parts[i:])
    return name, None


def norm(name: str) -> str:
    s = name.replace("UEFA", "УЕФА").translate(HOMOGLYPH).casefold()
    return re.sub(r"[\s\-–.]+", " ", s).strip()


def merge_leagues(s, dry: bool) -> dict:
    leagues = s.execute(text("SELECT id, country, name, gender, age_group FROM leagues WHERE sport_id = :sp"),
                        {"sp": SPORT}).all()
    fd_ids = {i for (i,) in s.execute(text("SELECT entity_id FROM source_entities WHERE source_id = :fd "
                                           "AND entity_type = 'league'"), {"fd": FD})}
    counts = dict(s.execute(text("SELECT league_id, COUNT(*) FROM matches WHERE sport_id = :sp GROUP BY 1"),
                            {"sp": SPORT}).all())
    groups = defaultdict(list)
    base_name = {}
    for lid, country, name, gender, age in leagues:
        if lid in fd_ids:
            continue
        base, _ = split_stage(name)
        base_name[lid] = base
        groups[(country, gender, age, norm(base))].append(lid)
    target = {}
    for ids in groups.values():
        keep = max(ids, key=lambda i: (counts.get(i, 0), -i))
        for i in ids:
            target[i] = keep
    moved = sum(1 for i, k in target.items() if i != k)
    stats = {"leagues": len(leagues), "olimp leagues": len(target), "merged into": len(set(target.values())),
             "moved": moved}
    if dry:
        return stats
    # stage of every Olimp match from its own tournament (source key 'tournament|dt|home|away')
    rows = s.execute(text("SELECT entity_id, split_part(external_id, '|', 1) FROM source_entities "
                          "WHERE source_id = :ol AND entity_type = 'match' AND method = 'provider'"),
                     {"ol": OLIMP}).all()
    stage_rows = []
    for mid, tourn in rows:
        _, stage = split_stage(". ".join(tourn.split(". ")[1:]))
        if stage:
            stage_rows.append({"m": mid, "st": stage[:64]})
    s.execute(text("CREATE TEMP TABLE stage_map (m bigint, st text) ON COMMIT DROP"))
    s.execute(text("CREATE TEMP TABLE league_map (o int, k int) ON COMMIT DROP"))
    if stage_rows:
        s.execute(text("INSERT INTO stage_map VALUES (:m, :st)"), stage_rows)
        s.execute(text("ANALYZE stage_map"))
        s.execute(text("UPDATE matches m SET stage = sm.st FROM stage_map sm WHERE m.id = sm.m "
                       "AND m.stage IS DISTINCT FROM sm.st"))
    moves = [{"o": o, "k": k} for o, k in target.items() if o != k]
    if moves:
        s.execute(text("INSERT INTO league_map VALUES (:o, :k)"), moves)
        s.execute(text("ANALYZE league_map"))
        s.execute(text("UPDATE matches m SET league_id = lm.k FROM league_map lm WHERE m.league_id = lm.o"))
        s.execute(text("UPDATE source_entities se SET entity_id = lm.k FROM league_map lm "
                       "WHERE se.entity_type = 'league' AND se.entity_id = lm.o"))
    s.execute(text("DELETE FROM leagues l WHERE sport_id = :sp AND NOT EXISTS (SELECT 1 FROM matches m "
                   "WHERE m.league_id = l.id) AND NOT EXISTS (SELECT 1 FROM source_entities se "
                   "WHERE se.entity_type = 'league' AND se.entity_id = l.id AND se.source_id = :fd)"),
              {"sp": SPORT, "fd": FD})
    s.execute(text("UPDATE leagues l SET name = left(v.n, 160) FROM (SELECT unnest(CAST(:ids AS int[])) AS id, "
                   "unnest(CAST(:names AS text[])) AS n) v WHERE l.id = v.id"),
              {"ids": list(set(target.values())), "names": [base_name[k] for k in set(target.values())]})
    stats["stages set"] = len(stage_rows)
    return stats


def merge_duplicates(s, dry: bool) -> dict:
    s.execute(text("""
        CREATE TEMP TABLE dup ON COMMIT DROP AS
        WITH fdm AS (SELECT entity_id FROM source_entities WHERE source_id = :fd AND entity_type = 'match'),
        pairs AS (
          SELECT m1.id AS a, m2.id AS b,
                 (m1.id IN (SELECT entity_id FROM fdm))::int AS a_fd, (m2.id IN (SELECT entity_id FROM fdm))::int AS b_fd
          FROM matches m1 JOIN matches m2 ON m2.home_team_id = m1.home_team_id AND m2.away_team_id = m1.away_team_id
           AND m2.id > m1.id AND m2.start_time BETWEEN m1.start_time - interval '24 hours'
                                                   AND m1.start_time + interval '24 hours'
           AND m2.home_score IS NOT DISTINCT FROM m1.home_score AND m2.away_score IS NOT DISTINCT FROM m1.away_score
          WHERE m1.sport_id = :sp)
        SELECT DISTINCT ON (drop_id) CASE WHEN b_fd > a_fd THEN b ELSE a END AS keep_id,
                                     CASE WHEN b_fd > a_fd THEN a ELSE b END AS drop_id
        FROM pairs WHERE NOT (a_fd = 1 AND b_fd = 1)"""), {"fd": FD, "sp": SPORT})
    # a match both kept and dropped (chains of 3+ copies): resolve to the final keeper
    s.execute(text("""UPDATE dup d SET keep_id = d2.keep_id FROM dup d2 WHERE d.keep_id = d2.drop_id"""))
    s.execute(text("DELETE FROM dup WHERE keep_id = drop_id"))
    n = s.execute(text("SELECT COUNT(*) FROM dup")).scalar()
    if dry or not n:
        return {"duplicates": n}
    # events: keep the kept match's own Olimp events; take the dropped one's only if the keeper has none
    s.execute(text("""UPDATE match_events e SET match_id = d.keep_id FROM dup d WHERE e.match_id = d.drop_id
                      AND NOT EXISTS (SELECT 1 FROM match_events k WHERE k.match_id = d.keep_id
                                      AND k.source_id = e.source_id)"""))
    s.execute(text("""INSERT INTO match_statistics (match_id, source_id, stat, period, home_value, away_value)
                      SELECT d.keep_id, st.source_id, st.stat, st.period, st.home_value, st.away_value
                      FROM match_statistics st JOIN dup d ON d.drop_id = st.match_id
                      ON CONFLICT DO NOTHING"""))
    s.execute(text("UPDATE markets mk SET match_id = d.keep_id FROM dup d WHERE mk.match_id = d.drop_id "
                   "AND NOT EXISTS (SELECT 1 FROM markets k WHERE k.match_id = d.keep_id AND k.kind = mk.kind "
                   "AND k.period = mk.period AND k.line IS NOT DISTINCT FROM mk.line)"))
    s.execute(text("UPDATE source_entities se SET entity_id = d.keep_id, method = 'linked' FROM dup d "
                   "WHERE se.entity_type = 'match' AND se.entity_id = d.drop_id"))
    s.execute(text("DELETE FROM matches m USING dup d WHERE m.id = d.drop_id"))
    return {"duplicates merged": n}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    with session() as s:
        print("leagues:", merge_leagues(s, args.dry_run), flush=True)
        print("matches:", merge_duplicates(s, args.dry_run), flush=True)
        if args.dry_run:
            s.rollback()
    with session() as s:
        print("now:", s.execute(text("SELECT (SELECT COUNT(*) FROM leagues WHERE sport_id = 1), "
                                     "(SELECT COUNT(*) FROM matches WHERE sport_id = 1)")).one())


if __name__ == "__main__":
    main()
