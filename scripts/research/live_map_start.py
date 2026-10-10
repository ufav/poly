"""Prices around the start of map N in a live_monitor recording.

  python scripts/research/live_map_start.py 10099082 3
"""

import json
import sys
from pathlib import Path

cs_id, map_no = sys.argv[1], sys.argv[2]
path = Path(__file__).resolve().parents[2] / "data" / "live" / f"{cs_id}.jsonl"
recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
print(f"records: {len(recs)}  from {recs[0]['t']} to {recs[-1]['t']}")

periods = []
for r in recs:
    p = (r.get("pm") or {}).get("period")
    if not periods or periods[-1][1] != p:
        periods.append((r["t"], p))
print("period changes:", periods)

start = next((i for i, r in enumerate(recs) if ((r.get("pm") or {}).get("period") or "").startswith(f"{map_no}/")), None)
if start is None:
    raise SystemExit(f"map {map_no} not found")
def show(r: dict) -> None:
    pm, cs = r.get("pm") or {}, r.get("cs") or {}
    cs_maps = [f"r{m['rounds']} {m['score']}" for m in cs.get("maps", []) if m["rounds"]]
    prices = " ".join(f"[{k}: {v['outcome']} {v['bid']}/{v['ask']}]" for k, v in pm.get("prices", {}).items())
    print(f"{r['t'][11:19]} {pm.get('period')} {pm.get('score')} | {prices} | cs {cs_maps[-1:] if cs_maps else cs.get('status')}")


for r in recs[max(0, start - 3): start + 8]:
    show(r)

n = int(map_no)
first_round = next((i for i, r in enumerate(recs)
                    if len([m for m in (r.get("cs") or {}).get("maps", []) if m["rounds"]]) >= n), None)
if first_round is not None:
    print(f"\nfirst rounds of map {map_no} in cybersport data:")
    for r in recs[max(0, first_round - 2): first_round + 3]:
        show(r)
print("\nlast records:")
for r in recs[-3:]:
    show(r)
