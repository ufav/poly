import json
import sys

import httpx

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept": "application/json",
}
API = "https://www.cybersport.ru/api/v1/match-stats/csgo"

match_id = sys.argv[1] if len(sys.argv) > 1 else "10099240"
dump = "--dump" in sys.argv

r = httpx.get(API, params={"filter[internalId]": match_id}, headers=UA, timeout=30)
r.raise_for_status()
doc = r.json()
if dump:
    print(json.dumps(doc, ensure_ascii=False, indent=1)[:6000])
    sys.exit()

data = doc["data"]
attrs = data["attributes"]
print("match:", attrs.get("externalName"), "| externalId:", attrs.get("externalId"), "| beginAt:", attrs.get("beginAt"))

included = doc.get("included", [])
by_key = {(x["type"], x["id"]): x for x in included}
teams = {x["id"]: x["attributes"].get("externalName") for x in included if x["type"] == "csgo_team"}
maps = {x["id"]: x["attributes"].get("name") or x["attributes"].get("externalName") for x in included if x["type"] == "csgo_map"}
team_ext = {x["attributes"].get("externalId"): x["attributes"].get("externalName") for x in included if x["type"] == "csgo_team"}
print("teams:", teams, "| by externalId:", team_ext)

games = [x for x in included if x["type"] == "csgo_match_game"]
for g in games:
    ga = g["attributes"]
    rounds = ga.get("rounds") or []
    if ga.get("isTotal"):
        continue
    map_name = maps.get(str(ga.get("mapId")), ga.get("mapId"))
    if not rounds:
        print(f"  game {g['id']} map={map_name}: no rounds", {k: v for k, v in ga.items() if k != "rounds"})
        continue
    score = {}
    half = None
    for rd in sorted(rounds, key=lambda x: x["round"]):
        w = rd["winner_team"]
        score[w] = score.get(w, 0) + 1
        if rd["round"] == 12:
            half = dict(score)
            first_ct = rd["ct"]
    name = lambda tid: teams.get(str(tid)) or team_ext.get(str(tid), tid)
    fmt = lambda s: " - ".join(f"{name(k)} {v}" for k, v in s.items())
    print(f"  map={map_name} rounds={len(rounds)} final: {fmt(score)}")
    if half:
        print(f"     1st half: {fmt(half)} | CT in 1st half: {name(first_ct)}")
    print("     sample round:", rounds[0])
