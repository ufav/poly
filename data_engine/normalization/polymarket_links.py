"""Polymarket matches / teams -> the same matches / teams of the results sources, by vote.

A Polymarket match that should be over (start + 3 h in the past) and still has no result is compared with the
finished matches of the same sport within +-36 h: both sides must have similar names (same words, one name's words
inside the other's - 'Suns' / 'Phoenix Suns', 'Elena Rybakina' / 'Rybakina Elena' - or a shared long word), the
best candidate must be unique. Each such pair is a vote 'Polymarket team -> our team'; a team created by the
Polymarket collector (no other source knows it) is merged into the target of its votes when they agree. Then the
two match rows are merged: same orientation -> the finished match stays and gets the Polymarket markets (odds of a
market already there are moved into it); swapped sides (tennis) -> the Polymarket row stays and takes the result,
statistics and mappings with home / away swapped.

  python -m data_engine.normalization.polymarket_links [--dry]
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from collections import Counter, defaultdict

from sqlalchemy import text

from core.db import session
from data_engine.providers.polymarket_prices import CLUB_WORDS, norm_team_words

PM = 4
SPORTS = (1, 2, 3, 5)
WINDOW = dt.timedelta(hours=36)
GENERIC = {"united", "city", "real", "sporting", "athletic", "club", "saint", "santa", "san", "los", "las", "del",
           "new", "north", "south", "east", "west", "royal", "union", "young", "boys", "women", "reserves",
           "amateur", "amateurs", "youth", "academy", "juniors", "junior", "team", "stars"}


ALIASES = {"czechia": "czech republic", "turkiye": "turkey", "korea republic": "south korea",
           "republic of ireland": "ireland", "bosnia herzegovina": "bosnia and herzegovina", "usa": "united states"}


def words(name: str) -> frozenset[str]:
    key = re.sub(r"[^a-z ]+", "", norm_team_words(name)).strip()
    if key in ALIASES:
        name = ALIASES[key]
    return frozenset(w for w in re.split(r"[^a-z0-9]+", norm_team_words(name)) if w and w not in CLUB_WORDS)


def initials(x: frozenset[str], y: frozenset[str]) -> bool:
    """'Hudd E.' vs 'Emile Hudd': the full words of x are in y and each initial starts another word of y."""
    full, init = {w for w in x if len(w) > 1}, [w for w in x if len(w) == 1]
    rest = y - full
    return bool(full and init) and full <= y and all(any(r.startswith(i) for r in rest) for i in init)


def similar(a: set[frozenset], b: set[frozenset]) -> int:
    """2 = same words / one inside the other, 1 = a shared distinctive word, 0 = different."""
    best = 0
    for x in a:
        for y in b:
            if x and y and (x <= y or y <= x or initials(x, y) or initials(y, x)):
                return 2
            if any(len(w) >= 4 and w not in GENERIC for w in x & y):
                best = 1
    return best


def side_score(t1: int, t2: int, names: dict[int, set[frozenset]]) -> int:
    return 3 if t1 == t2 else similar(names.get(t1, set()), names.get(t2, set()))


def pairs(s, now: dt.datetime) -> list[tuple[dict, dict, bool, tuple[int, int]]]:
    """(Polymarket match, finished match, swapped, side scores) of the unique best candidates."""
    pm = [dict(r._mapping) for r in s.execute(text("""
        SELECT m.id, m.sport_id, m.start_time, m.home_team_id h, m.away_team_id a FROM matches m
        WHERE m.status = 'scheduled' AND m.start_time < :cut AND m.sport_id = ANY(:sports)
          AND EXISTS (SELECT 1 FROM source_entities se WHERE se.source_id = :pm AND se.entity_type = 'match'
                      AND se.entity_id = m.id)"""),
        {"cut": now - dt.timedelta(hours=3), "pm": PM, "sports": list(SPORTS)})]
    if not pm:
        return []
    lo = min(m["start_time"] for m in pm) - WINDOW
    hi = max(m["start_time"] for m in pm) + WINDOW
    fin = [dict(r._mapping) for r in s.execute(text("""
        SELECT id, sport_id, start_time, home_team_id h, away_team_id a FROM matches
        WHERE status = 'finished' AND sport_id = ANY(:sports) AND start_time BETWEEN :lo AND :hi"""),
        {"lo": lo, "hi": hi, "sports": list(SPORTS)})]
    team_ids = {m[k] for m in pm + fin for k in ("h", "a")}
    meta = {tid: (g, a, p) for tid, g, a, p in s.execute(text(
        "SELECT id, gender, age_group, kind = 'pair' FROM teams WHERE id = ANY(:ids)"), {"ids": list(team_ids)})}
    names: dict[int, set[frozenset]] = defaultdict(set)
    for tid, n in s.execute(text("""
            SELECT t.id, t.name FROM teams t WHERE t.id = ANY(:ids)
            UNION SELECT se.entity_id, se.external_name FROM source_entities se
            WHERE se.entity_type = 'team' AND se.entity_id = ANY(:ids) AND se.external_name IS NOT NULL"""),
                            {"ids": list(team_ids)}):
        names[tid].add(words(n))
    by_day = defaultdict(list)
    for f in fin:
        by_day[(f["sport_id"], f["start_time"].astimezone(dt.timezone.utc).date())].append(f)
    out = []
    for m in pm:
        cands = []
        for d in (-1, 0, 1, 2):
            for f in by_day.get((m["sport_id"], m["start_time"].astimezone(dt.timezone.utc).date() + dt.timedelta(days=d)), []):
                if abs(f["start_time"] - m["start_time"]) > WINDOW:
                    continue
                if {meta.get(f["h"]), meta.get(f["a"])} != {meta.get(m["h"]), meta.get(m["a"])}:
                    continue  # women / youth teams of the same club, singles player vs a pair
                straight = (side_score(m["h"], f["h"], names), side_score(m["a"], f["a"], names))
                swapped = (side_score(m["h"], f["a"], names), side_score(m["a"], f["h"], names))
                for sc, sw in ((straight, False), (swapped, True)):
                    if min(sc) > 0:
                        cands.append((sum(sc), f, sw, sc))
        if not cands:
            continue
        cands.sort(key=lambda c: -c[0])
        if len(cands) == 1 or cands[0][0] > cands[1][0]:
            out.append((m, cands[0][1], cands[0][2], cands[0][3]))
    return out


def merge_teams(s, votes: dict[int, Counter], strong: dict[int, set[int]], anchored: dict[int, set[int]],
                dry: bool) -> tuple[int, int]:
    """Merge Polymarket-only teams into the agreed target: votes agree, and the names are close (same words / one
    inside the other / initials) or a shared word is backed by at least two votes. A Polymarket team the collector
    matched by name to a team of ours ('Aston Villa' alias 'Villa' -> some 'Villa') is re-pointed when strong votes
    (two, or one with the other side already the same team) agree on another one."""
    pm_only = {r[0] for r in s.execute(text("""
        SELECT entity_id FROM source_entities WHERE entity_type = 'team' GROUP BY entity_id
        HAVING bool_and(source_id = :pm)"""), {"pm": PM})}
    pm_keys = Counter(r[0] for r in s.execute(text(
        "SELECT entity_id FROM source_entities WHERE source_id = :pm AND entity_type = 'team' AND method = 'name'"),
        {"pm": PM}))
    merged = remapped = 0
    for tid, c in votes.items():
        (target, n), *rest = c.most_common()
        if target in pm_only or (rest and rest[0][1] * 2 > n):
            continue
        if tid not in pm_only:
            if pm_keys.get(tid) == 1 and target in strong.get(tid, set()) and (n >= 2 or target in anchored[tid]):
                remapped += 1
                print(f"  remap {tid} -> {target} ({n} vote{'s' * (n > 1)})")
                if not dry:
                    remap(s, tid, target)
            continue
        if target not in strong.get(tid, set()) and n < 2:
            continue
        a, b = s.execute(text("SELECT name FROM teams WHERE id IN (:a, :b) ORDER BY id = :a DESC"),
                         {"a": tid, "b": target}).scalars().all()
        print(f"  team {a!r} -> {b!r} ({n} vote{'s' * (n > 1)})")
        merged += 1
        if dry:
            continue
        s.execute(text("UPDATE matches SET home_team_id = :b WHERE home_team_id = :a"), {"a": tid, "b": target})
        s.execute(text("UPDATE matches SET away_team_id = :b WHERE away_team_id = :a"), {"a": tid, "b": target})
        s.execute(text("UPDATE source_entities SET entity_id = :b, method = 'vote' WHERE entity_type = 'team' "
                       "AND entity_id = :a"), {"a": tid, "b": target})
        s.execute(text("DELETE FROM teams WHERE id = :a"), {"a": tid})
    return merged, remapped


def remap(s, tid: int, target: int) -> None:
    """The Polymarket key of team tid -> target, in the Polymarket matches that have no result yet."""
    for side in ("home_team_id", "away_team_id"):
        s.execute(text(f"""UPDATE matches m SET {side} = :b WHERE {side} = :a AND m.status = 'scheduled'
            AND EXISTS (SELECT 1 FROM source_entities se WHERE se.source_id = :pm AND se.entity_type = 'match'
                        AND se.entity_id = m.id)"""), {"a": tid, "b": target, "pm": PM})
    s.execute(text("UPDATE source_entities SET entity_id = :b, method = 'vote' WHERE source_id = :pm "
                   "AND entity_type = 'team' AND entity_id = :a"), {"a": tid, "b": target, "pm": PM})


def move_markets(s, src: int, dst: int) -> None:
    """Markets of match src -> match dst; a market dst already has absorbs the odds."""
    for mk, kind, period, line, subj in s.execute(text(
            "SELECT id, kind, period, line, subject FROM markets WHERE match_id = :m"), {"m": src}).all():
        twin = s.scalar(text("SELECT id FROM markets WHERE match_id = :d AND kind = :k AND period = :p "
                             "AND line IS NOT DISTINCT FROM :l AND subject IS NOT DISTINCT FROM :su"),
                        {"d": dst, "k": kind, "p": period, "l": line, "su": subj})
        if twin:
            s.execute(text("UPDATE odds_history SET market_id = :t WHERE market_id = :m"), {"t": twin, "m": mk})
            s.execute(text("UPDATE markets SET extra = COALESCE(extra, '{}'::jsonb) || "
                           "COALESCE((SELECT extra FROM markets WHERE id = :m), '{}'::jsonb) WHERE id = :t"),
                      {"t": twin, "m": mk})
            s.execute(text("DELETE FROM markets WHERE id = :m"), {"m": mk})
        else:
            s.execute(text("UPDATE markets SET match_id = :d WHERE id = :m"), {"d": dst, "m": mk})


SWAP_EXTRA = """(SELECT COALESCE(jsonb_object_agg(
    CASE k WHEN 'home_red' THEN 'away_red' WHEN 'away_red' THEN 'home_red' ELSE k END,
    CASE WHEN jsonb_typeof(v) = 'array' AND jsonb_array_length(v) = 2 THEN jsonb_build_array(v->1, v->0) ELSE v END),
    NULL) FROM jsonb_each(CASE WHEN jsonb_typeof(f.result_extra) = 'object' THEN f.result_extra END) e(k, v))"""


def merge_match(s, m: int, f: int, swapped: bool) -> None:
    if not swapped:  # the finished row stays
        move_markets(s, m, f)
        s.execute(text("UPDATE source_entities SET entity_id = :f WHERE entity_type = 'match' AND entity_id = :m"),
                  {"f": f, "m": m})
        s.execute(text("DELETE FROM matches WHERE id = :m"), {"m": m})
        return
    # the Polymarket row stays (its markets are oriented to it), the result comes in with home / away swapped
    s.execute(text(f"""
        UPDATE matches t SET status = f.status, home_score = f.away_score, away_score = f.home_score,
          periods = (SELECT jsonb_agg(jsonb_build_array(p->1, p->0) ORDER BY n) FROM
                     jsonb_array_elements(CASE WHEN jsonb_typeof(f.periods) = 'array' THEN f.periods END) WITH ORDINALITY x(p, n)),
          result_extra = {SWAP_EXTRA}, league_id = f.league_id, stage = f.stage
        FROM matches f WHERE t.id = :m AND f.id = :f"""), {"m": m, "f": f})
    s.execute(text("UPDATE match_statistics SET match_id = :m, home_value = away_value, away_value = home_value "
                   "WHERE match_id = :f"), {"m": m, "f": f})
    s.execute(text("UPDATE match_events SET match_id = :m, side = CASE side WHEN 'h' THEN 'a' WHEN 'a' THEN 'h' "
                   "ELSE side END WHERE match_id = :f"), {"m": m, "f": f})
    s.execute(text("UPDATE source_entities SET entity_id = :m WHERE entity_type = 'match' AND entity_id = :f"),
              {"f": f, "m": m})
    s.execute(text("DELETE FROM matches WHERE id = :f"), {"f": f})


def main(dry: bool = False) -> dict:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # names with diacritics on a Windows console
    now = dt.datetime.now(dt.timezone.utc)
    cnt = Counter()
    with session() as s:
        found = pairs(s, now)
        votes: dict[int, Counter] = defaultdict(Counter)
        strong: dict[int, set[int]] = defaultdict(set)
        anchored: dict[int, set[int]] = defaultdict(set)  # strong, and the other side is already the same team
        for m, f, sw, sc in found:
            for k, (pm_side, our) in enumerate((("h", "a" if sw else "h"), ("a", "h" if sw else "a"))):
                if m[pm_side] != f[our]:
                    votes[m[pm_side]][f[our]] += 1
                    if sc[k] >= 2:
                        strong[m[pm_side]].add(f[our])
                    if sc[k] >= 2 and sc[1 - k] == 3:
                        anchored[m[pm_side]].add(f[our])
        cnt["pairs"] = len(found)
        cnt["teams"], cnt["remapped"] = merge_teams(s, votes, strong, anchored, dry)
        if dry:
            s.rollback()
            print(f"polymarket links (dry): {dict(cnt)}")
            return dict(cnt)
        s.flush()
        # after the team merge: pairs whose sides are now the same teams
        for m, f, sw, _ in pairs(s, now):
            row = s.execute(text("SELECT a.home_team_id, a.away_team_id, b.home_team_id, b.away_team_id, "
                                 "(SELECT count(*) FROM markets WHERE match_id = b.id) FROM matches a, matches b "
                                 "WHERE a.id = :m AND b.id = :f"), {"m": m["id"], "f": f["id"]}).one()
            same = (row[0], row[1]) == (row[2], row[3])
            swap = (row[0], row[1]) == (row[3], row[2])
            if not (same or swap) or (swap and row[4]):
                cnt["unmerged"] += 1
                continue
            merge_match(s, m["id"], f["id"], swapped=not same)
            cnt["matches"] += 1
    print(f"polymarket links: {dict(cnt)}")
    return dict(cnt)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    main(ap.parse_args().dry)
