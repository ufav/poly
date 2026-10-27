"""CS2: clear favourite (match prematch on Polymarket) down at the start of a map (0-3 ... 0-6, 1-4 ... 1-6):
how often does he still win the map. Score = state passed on the way (one map counts in every state it went through).

  python analysis/fav_slow_start.py [--tier1]
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmbot import storage  # noqa: E402
from pmbot.join import norm_team  # noqa: E402
from pmbot.tiers import tier_of  # noqa: E402
from pistol_stats import prematch_prices  # noqa: E402

STATES = [(0, 3), (0, 4), (0, 5), (0, 6), (1, 4), (1, 5), (1, 6)]  # (fav, dog)
FAV_BANDS = [(0.60, 0.70), (0.70, 0.80), (0.80, 0.98)]


def band_of(p: float) -> tuple[float, float] | None:
    return next((b for b in FAV_BANDS if b[0] <= p < b[1]), None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier1", action="store_true", help="tier1 tournaments only")
    args = ap.parse_args()
    conn = storage.connect()

    maps = conn.execute(
        """SELECT g.id, g.number, c.external_id, c.tournament FROM cs_games g JOIN cs_matches c ON c.id = g.match_id
           WHERE g.rounds >= 13 AND c.external_id IS NOT NULL""").fetchall()
    rounds = defaultdict(list)
    for game_id, rnd, winner in conn.execute("SELECT game_id, round, winner_team FROM cs_rounds ORDER BY game_id, round"):
        rounds[game_id].append((rnd, winner))

    pm_cache: dict = {}
    # (band, state) -> [maps, fav won]; band None = all favourites >= 0.60
    stat = defaultdict(lambda: [0, 0])
    base = defaultdict(lambda: [0, 0])
    n_maps = 0
    for game_id, number, external_id, tournament in maps:
        tier = tier_of(tournament, "cs2")
        if args.tier1 and tier != "tier1":
            continue
        rs = rounds.get(game_id)
        if not rs or rs[0][0] != 1 or len(rs) < 13:
            continue
        if external_id not in pm_cache:
            pm_cache[external_id] = prematch_prices(conn, external_id)
        pm = pm_cache[external_id]
        if not pm:
            continue
        pm_n = {norm_team(k): v for k, v in pm.items()}
        fav, fav_p = max(pm_n.items(), key=lambda kv: kv[1])
        band = band_of(fav_p)
        if not band:
            continue
        teams = {norm_team(w) for _, w in rs}
        if fav not in teams or len(teams) != 2:
            continue
        n_maps += 1
        score, seen = [0, 0], set()
        for _, winner in rs:
            score[0 if norm_team(winner) == fav else 1] += 1
            seen.add(tuple(score))
        won = score[0] > score[1]
        for b in (band, None):
            base[b][0] += 1
            base[b][1] += won
            for st in STATES:
                if st in seen:
                    stat[(b, st)][0] += 1
                    stat[(b, st)][1] += won

    print(f"maps with rounds and Polymarket favourite >= 0.60{' (tier1)' if args.tier1 else ''}: {n_maps}\n")
    cols = [None, *FAV_BANDS]
    names = ["all fav", *(f"{lo:.2f}-{hi:.2f}" if hi < 0.98 else f"{lo:.2f}+" for lo, hi in FAV_BANDS)]
    print(f"{'fav-dog':>8} " + " ".join(f"{n:>18}" for n in names))
    for label, row in [("any", None), *((f"{a}-{b}", (a, b)) for a, b in STATES)]:
        cells = []
        for b in cols:
            n, w = base[b] if row is None else stat[(b, row)]
            cells.append(f"{100 * w / n:5.1f}% (n={n:4d})" if n else f"{'—':>18}")
        print(f"{label:>8} " + " ".join(f"{c:>18}" for c in cells))


if __name__ == "__main__":
    main()
