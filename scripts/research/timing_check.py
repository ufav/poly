"""Check pmbot/maptiming.py on a recorded live match: estimated map start / halftime vs the real
round score snapshots in data/live/<cs_id>.jsonl (Polymarket `score`, refreshed every ~5 min).

  python scripts/research/timing_check.py 10099082
"""

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot.http import HttpClient  # noqa: E402
from pmbot.maptiming import map_window  # noqa: E402
from pmbot.polymarket import CLOB, GAMMA  # noqa: E402

cs_id = sys.argv[1]
recs = [json.loads(x) for x in (Path(__file__).resolve().parents[2] / "data" / "live" / f"{cs_id}.jsonl")
        .read_text(encoding="utf-8").splitlines() if x.strip()]


def ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s).timestamp())


def hm(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%H:%M:%S")


# real round snapshots per map: (time, rounds played, score)
snaps: dict[int, list] = {}
for r in recs:
    pm = r.get("pm") or {}
    period, score = pm.get("period") or "", pm.get("score") or ""
    if "/" not in period or "|" not in score:
        continue
    rnd = score.split("|")[0]
    if rnd == "000-000" or "-" not in rnd:
        continue
    a, b = (int(x) for x in rnd.split("-"))
    snaps.setdefault(int(period.split("/")[0]), []).append((ts(r["t"]), a + b, rnd))

http = HttpClient()
title_parts = None
events = [e for closed in ("true", "false") for off in range(0, 500, 100)
          for e in http.get_json(f"{GAMMA}/events", {"series_id": 10310, "closed": closed, "limit": 100, "offset": off,
                                                     "start_date_min": "2026-09-25"})]
event = max((e for e in events if "aurora" in e["title"].lower() and "vitality" in e["title"].lower()),
            key=lambda e: e.get("startDate") or "")
print(event["title"], event.get("score"))
start = int(dt.datetime.fromisoformat(event["startDate"].replace("Z", "+00:00")).timestamp())
lower = start - 600
for m in sorted(event["markets"], key=lambda m: m.get("groupItemTitle") or ""):
    title = m.get("groupItemTitle") or ""
    if m.get("sportsMarketType") not in ("moneyline", "child_moneyline"):
        continue
    token = json.loads(m["clobTokenIds"])[0]
    a_won = json.loads(m["outcomePrices"])[0] in ("1", "1.0")
    hist = http.get_json(f"{CLOB}/prices-history", {"market": token, "startTs": start - 3600,
                                                    "endTs": start + 6 * 3600, "fidelity": 1})["history"]
    series = [(int(x["t"]), float(x["p"])) for x in hist]
    print(f"\n{title or 'Match'}: {len(series)} points")
    w = map_window(series, lower, a_won)
    if not w:
        print("   no window")
        continue
    t0, t1 = w
    num = int(title.split()[1]) if title.startswith("Map") else 3
    s = snaps.get(num, [])
    print(f"   estimated start {hm(t0)}  end {hm(t1)}  ({(t1 - t0) / 60:.0f} min)")
    for t, n, rnd in s:
        print(f"   real snapshot {hm(t)}: {rnd} ({n} rounds)")
    if s:
        total = max(n for _, n, _ in s)
        print(f"   NOTE: rounds in last snapshot = {total}; pass the final total to compare estimated half")
    lower = t1
