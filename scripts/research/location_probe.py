"""Which location fields cybersport.ru exposes: team country, tournament city / LAN / online."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot.cybersport import API, CS2_DISCIPLINE_ID
from pmbot.http import HttpClient


def show(label: str, obj: dict) -> None:
    print(f"--- {label} {obj['type']}#{obj['id']}")
    print(json.dumps(obj.get("attributes"), ensure_ascii=False)[:1500])
    print("rels:", list((obj.get("relationships") or {}).keys()))


http = HttpClient(min_interval=0.3)
doc = http.get_json(f"{API}/matches", {"filter[date]": "2026-03-01", "filter[disciplineIds]": CS2_DISCIPLINE_ID,
                                        "page[limit]": 100})
seen = set()
for x in doc.get("included", []):
    if x["type"] not in seen:
        seen.add(x["type"])
        show("list", x)
print("match rels:", list(doc["data"][0].get("relationships", {}).keys()))
print("match attrs:", list(doc["data"][0]["attributes"].keys()))

stage_ref = doc["data"][0]["relationships"]["stage"]["data"]
for path in (f"stages/{stage_ref['id']}", f"tournaments/{stage_ref['id']}"):
    r = http.get(f"{API}/{path}")
    print(f"\n=== GET /{path} -> {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        show("detail", d["data"])
        for x in d.get("included", [])[:10]:
            show("inc", x)
