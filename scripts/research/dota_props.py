"""Dota 2 prop markets on Polymarket: how often each type resolves YES, kill lines, volumes (closed events, cached).

  python scripts/research/dota_props.py [--refresh]
"""

import datetime as dt
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot import polymarket  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402

CACHE = Path(__file__).resolve().parents[2] / "data" / "dota_events.json"


def load(refresh: bool) -> list[dict]:
    if CACHE.exists() and not refresh:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    now = dt.datetime.now(dt.timezone.utc)
    events = list(polymarket.iter_closed_events(HttpClient(min_interval=0.3), now - dt.timedelta(days=730), now,
                                                step=dt.timedelta(days=30), series_id=polymarket.DOTA2_SERIES_ID))
    CACHE.write_text(json.dumps(events), encoding="utf-8")
    return events


def main() -> None:
    events = load("--refresh" in sys.argv)
    by_type = defaultdict(list)  # type -> [(yes resolved, volume, question)]
    for e in events:
        for m in e.get("markets", []):
            prices = json.loads(m.get("outcomePrices") or "[]")
            outcomes = json.loads(m.get("outcomes") or "[]")
            if len(prices) != 2 or prices[0] not in ("0", "1"):
                continue
            by_type[m.get("sportsMarketType")].append((prices[0] == "1", float(m.get("volume") or 0),
                                                       m.get("question") or "", outcomes))
    print(f"closed events: {len(events)}\n")
    print(f"{'type':28} {'markets':>7} {'first outcome won':>17} {'median vol':>10} {'total vol':>10}  outcomes")
    for t, xs in sorted(by_type.items(), key=lambda kv: -sum(v for _, v, _, _ in kv[1])):
        print(f"{str(t):28} {len(xs):7d} {sum(y for y, _, _, _ in xs) / len(xs):17.1%} "
              f"{statistics.median(v for _, v, _, _ in xs):10.0f} {sum(v for _, v, _, _ in xs) / 1e6:9.1f}M  {xs[0][3]}")
    kills = by_type.get("kill_over_under_game", [])
    lines = defaultdict(list)
    for yes, _, q, _ in kills:
        try:
            lines[float(q.split("Over/Under")[1].split()[0])].append(yes)
        except (IndexError, ValueError):
            pass
    print("\nkills O/U per game: line -> markets, over share")
    for line in sorted(lines):
        if len(lines[line]) >= 30:
            print(f"  {line:5.1f}: {len(lines[line]):5d}  over {sum(lines[line]) / len(lines[line]):.1%}")


if __name__ == "__main__":
    main()
