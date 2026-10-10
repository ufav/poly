import datetime as dt
import json

import httpx

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
c = httpx.Client(headers=UA, timeout=30)
ts = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")

print("== 1. Earliest / latest closed CS2 events on Polymarket")
first = c.get(f"{GAMMA}/events", params={"series_id": 10310, "closed": "true", "limit": 3, "order": "startDate", "ascending": "true"}).json()
for e in first:
    print("  earliest:", e.get("startTime") or e.get("startDate"), e["title"][:80])
keys_sample = first[0]
print("  event keys with 'id'/'game':", [k for k in keys_sample if "id" in k.lower() or "game" in k.lower()])

print("\n== 2. Recent closed big match: price history granularity")
ev = c.get(f"{GAMMA}/events", params={"slug": "cs2-ksm-ff-2026-04-20"}).json()
if not ev:
    ev = c.get(f"{GAMMA}/events", params={"series_id": 10310, "closed": "true", "limit": 1, "order": "volume", "ascending": "false"}).json()
e = ev[0]
print("  event:", e["title"][:80], "| start:", e.get("startTime"), "| gameId:", e.get("gameId"), "| score:", e.get("score"))
m = next(m for m in e["markets"] if m.get("groupItemTitle") == "Map 2 Winner")
tok = json.loads(m["clobTokenIds"])[0]
start = int(dt.datetime.fromisoformat(e["startTime"].replace("Z", "+00:00")).timestamp())
for fid in (1, 5):
    h = c.get(f"{CLOB}/prices-history", params={"market": tok, "startTs": start - 1800, "endTs": start + 4 * 3600, "fidelity": fid}).json().get("history", [])
    print(f"  prices-history fidelity={fid} window 4.5h: {len(h)} points", (ts(h[0]['t']), ts(h[-1]['t'])) if h else "")

print("\n== 3. Data API trades for that market")
r = c.get(f"{DATA}/trades", params={"market": m["conditionId"], "limit": 500})
print("  status", r.status_code)
if r.status_code == 200:
    tr = r.json()
    tr = tr.get("data", tr) if isinstance(tr, dict) else tr
    print("  trades:", len(tr))
    if tr:
        print("  sample:", {k: tr[0].get(k) for k in ("timestamp", "price", "size", "side", "outcome")})
        tss = sorted(t["timestamp"] for t in tr)
        print("  range:", ts(tss[0]), "->", ts(tss[-1]))

print("\n== 4. cybersport.ru: old match stats availability")
for date in ("2025-10-12", "2026-01-18", "2026-04-20"):
    html = c.get("https://www.cybersport.ru/matches/cs2", params={"date": date}).text
    import re
    ids = list(dict.fromkeys(re.findall(r"/matches/cs2/(\d+)", html)))
    ok = 0
    sample = None
    for mid in ids[:6]:
        rr = c.get("https://www.cybersport.ru/api/v1/match-stats/csgo", params={"filter[internalId]": mid}, headers={**UA, "Accept": "application/json"})
        if rr.status_code == 200:
            ok += 1
            sample = sample or rr.json()["data"]["attributes"]
    print(f"  {date}: matches on page={len(ids)}, stats ok {ok}/{min(6, len(ids))}", "| sample:", sample)
