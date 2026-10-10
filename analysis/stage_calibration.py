"""Pre-match calibration of the match-winner price by tournament stage and format (Polymarket titles).

  python analysis/stage_calibration.py [--min-volume 2000]
"""

import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage  # noqa: E402


def stage_of(tournament: str | None) -> str:
    t = (tournament or "").lower()
    if "qualifier" in t or "qualification" in t:
        return "qualifier"
    if "grand final" in t or t.endswith(" final") or t.endswith(" finals"):
        return "final"
    if "playoff" in t or "play-off" in t:
        return "playoffs"
    if "play-in" in t:
        return "play-in"
    if any(k in t for k in ("group", "swiss", "stage", "regular", "round robin", "league")):
        return "groups/league"
    return "other"


TIER1_KEYWORDS = ("iem", "blast", "esl pro league", "pgl", "major", "esports world cup", "starladder",
                  "perfect world", "fissure", "betboom dacha", "thunderpick world")


def tier_of(tournament: str | None) -> str:
    t = (tournament or "").lower()
    if "qualifier" in t:
        return "tier1-qualifier" if any(k in t for k in TIER1_KEYWORDS) else "other-qualifier"
    return "tier1" if any(k in t for k in TIER1_KEYWORDS) else "tier2+"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-volume", type=float, default=2000)
    args = ap.parse_args()
    conn = storage.connect()
    rows = conn.execute(
        """SELECT e.tournament, e.best_of, e.start_time, m.token_a, m.final_price_a, m.volume
           FROM pm_events e JOIN pm_markets m ON m.event_id = e.id AND m.market_type = 'moneyline'
           WHERE m.history_status = 'done' AND m.closed = 1 AND m.final_price_a IN (0, 1)
             AND m.volume >= ? AND e.start_time < ?""",
        (args.min_volume, dt.datetime.now(dt.timezone.utc).isoformat()),
    ).fetchall()

    agg = defaultdict(lambda: [0, 0, 0.0])
    strong = defaultdict(lambda: [0, 0, 0.0])
    for tournament, best_of, start_time, token, final_a, volume in rows:
        start = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
        pre = conn.execute("SELECT p FROM pm_prices WHERE token_id = ? AND t <= ? ORDER BY t DESC LIMIT 1",
                           (token, start)).fetchone()
        if not pre or pre[0] in (0, 1):
            continue
        p, won = (pre[0], final_a == 1) if pre[0] >= 0.5 else (1 - pre[0], final_a == 0)
        volume_bucket = "vol>=50k (post-hoc, biased)" if volume >= 50000 else "vol<50k (post-hoc, biased)"
        tier = tier_of(tournament)
        for key in (("stage", stage_of(tournament)), ("format", f"BO{best_of}"), ("volume", volume_bucket),
                    ("tier", tier), ("tier x stage", f"{tier} / {stage_of(tournament)}")):
            for target in (agg,) + ((strong,) if p >= 0.70 else ()):
                a = target[key]
                a[0] += 1
                a[1] += won
                a[2] += p

    for title, data in (("ALL MATCHES: favorite price at start vs win rate", agg),
                        ("CLEAR FAVORITES (price >= 0.70)", strong)):
        print(title)
        print(f"   {'group':45} {'n':>6} {'avg price':>10} {'win rate':>9} {'edge':>7} {'±2se':>6}")
        for key in sorted(data):
            n, w, s = data[key]
            if n >= 30:
                win = w / n
                se2 = 2 * (win * (1 - win) / n) ** 0.5
                print(f"   {key[0] + ': ' + key[1]:45} {n:6d} {s / n:10.3f} {win:9.1%} {win - s / n:+7.3f} {se2:6.3f}")
        print()


if __name__ == "__main__":
    main()
