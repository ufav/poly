import json
from collections import Counter

import httpx

c = httpx.Client(headers={"User-Agent": "Mozilla/5.0 Chrome/129.0", "Accept": "application/json"}, timeout=30)
API = "https://www.cybersport.ru/api"

for date in ("2026-10-09", "2026-01-18", "2025-10-12"):
    doc = c.get(f"{API}/matches", params={"filter[date]": date, "filter[disciplineIds]": 22, "page[limit]": 100}).json()
    disc = Counter(x["relationships"]["discipline"]["data"]["id"] for x in doc["data"])
    print(date, "matches:", len(doc["data"]), "disciplines:", dict(disc), "meta:", doc.get("meta"))

print("\n=== single match")
r = c.get(f"{API}/matches/10099240")
print(r.status_code, len(r.text))
if r.status_code == 200:
    doc = r.json()
    print("attributes:", json.dumps(doc["data"]["attributes"], ensure_ascii=False)[:600])
    print("relationships:", list(doc["data"].get("relationships", {})))
    print("included types:", Counter(x["type"] for x in doc.get("included", [])))
    for x in doc.get("included", []):
        if x["type"] in ("match_game", "game", "map", "match_participant"):
            print(" ", x["type"], x["id"], json.dumps(x["attributes"], ensure_ascii=False)[:300], json.dumps(x.get("relationships"), ensure_ascii=False)[:200])

print("\n=== list item with includes")
doc = c.get(f"{API}/matches", params={"filter[date]": "2026-10-10", "filter[disciplineIds]": 22}).json()
inc = {(x["type"], x["id"]): x for x in doc.get("included", [])}
m = next(x for x in doc["data"] if x["id"] == "10099240")
for rel in ("participant1", "participant2", "stage"):
    ref = m["relationships"][rel]["data"]
    obj = inc.get((ref["type"], ref["id"]))
    print(rel, json.dumps(obj, ensure_ascii=False)[:400])
