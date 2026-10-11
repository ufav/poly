"""Dota 2 on Polymarket: series id, events, market types, volumes, period; OpenDota availability.

  python scripts/research/dota_probe.py
"""

import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot import polymarket  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402
from pmbot.polymarket import GAMMA  # noqa: E402


def main() -> None:
    http = HttpClient(min_interval=0.3)
    sports = http.get_json(f"{GAMMA}/sports")
    for s in sports:
        if any(k in json.dumps(s).lower() for k in ("dota", "cs2", "csgo", "lol", "valorant")):
            print("sport:", {k: s.get(k) for k in ("sport", "series", "tags", "resolution")})
    dota = [s for s in sports if "dota" in str(s.get("sport", "")).lower()]
    if not dota:
        print("no dota sport entry")
        return
    series_id = int(str(dota[0]["series"]).split(",")[0])
    now = dt.datetime.now(dt.timezone.utc)
    for closed in ("true", "false"):
        if closed == "true":
            events = list(polymarket.iter_closed_events(http, now - dt.timedelta(days=730), now,
                                                        step=dt.timedelta(days=30), series_id=series_id))
        else:
            events = http.get_json(f"{GAMMA}/events", {"series_id": series_id, "closed": "false", "limit": 100})
        starts = sorted(e.get("startTime") or e.get("startDate") or "" for e in events)
        vols = sorted(float(e.get("volume") or 0) for e in events)
        types = Counter(m.get("sportsMarketType") for e in events for m in e.get("markets", []))
        print(f"\nseries {series_id} closed={closed}: {len(events)} events, {starts[0][:10] if starts else ''} .. "
              f"{starts[-1][:10] if starts else ''}")
        if vols:
            print(f"  volume median {vols[len(vols) // 2]:.0f}, p90 {vols[int(len(vols) * .9)]:.0f}, "
                  f"total {sum(vols) / 1e6:.1f}M; with gameId {sum(bool(e.get('gameId')) for e in events)}")
        print("  market types:", types.most_common(12))
        for e in sorted(events, key=lambda e: -float(e.get("volume") or 0))[:3]:
            print(f"  top: {e['title']} | vol {float(e.get('volume') or 0):.0f} | gameId {e.get('gameId')} | "
                  f"{[m.get('groupItemTitle') or m.get('sportsMarketType') for m in e.get('markets', [])][:8]}")

    pro = http.get_json("https://api.opendota.com/api/proMatches")
    print(f"\nOpenDota proMatches: {len(pro)} rows, sample keys {list(pro[0])[:14]}")
    m = http.get_json(f"https://api.opendota.com/api/matches/{pro[0]['match_id']}")
    print(f"  match {pro[0]['match_id']}: {pro[0].get('radiant_name')} vs {pro[0].get('dire_name')} "
          f"({pro[0].get('league_name')}), duration {m.get('duration')} s, "
          f"radiant_gold_adv points {len(m.get('radiant_gold_adv') or [])}, objectives {len(m.get('objectives') or [])}, "
          f"teamfights {len(m.get('teamfights') or [])}")


if __name__ == "__main__":
    main()
