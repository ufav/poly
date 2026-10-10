"""Poll cybersport.ru round data and Polymarket prices for one live CS2 match.

Usage: python live_monitor.py <cybersport_match_id> "<team A>" "<team B>" [interval_sec]
Writes JSONL to data/live/<cybersport_match_id>.jsonl
"""

import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept": "application/json",
}
CS_API = "https://www.cybersport.ru/api/v1/match-stats/csgo"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
CS2_SERIES_ID = 10310

cs_id, team_a, team_b = sys.argv[1], sys.argv[2].lower(), sys.argv[3].lower()
interval = float(sys.argv[4]) if len(sys.argv) > 4 else 30

out_path = Path(__file__).resolve().parents[2] / "data" / "live" / f"{cs_id}.jsonl"
out_path.parent.mkdir(parents=True, exist_ok=True)
client = httpx.Client(headers=UA, timeout=20)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")


def find_pm_event_slug() -> str:
    for offset in range(0, 1000, 100):
        events = client.get(
            f"{GAMMA}/events",
            params={"series_id": CS2_SERIES_ID, "closed": "false", "limit": 100, "offset": offset},
        ).json()
        for e in events:
            title = e["title"].lower()
            if team_a in title and team_b in title:
                return e["slug"]
        if len(events) < 100:
            break
    raise SystemExit(f"Polymarket event for {team_a} vs {team_b} not found")


def cybersport_state() -> dict:
    r = client.get(CS_API, params={"filter[internalId]": cs_id})
    if r.status_code != 200:
        return {"status": r.status_code}
    doc = r.json()
    teams = {x["id"]: x["attributes"].get("externalName") for x in doc.get("included", []) if x["type"] == "csgo_team"}
    maps = []
    for g in doc.get("included", []):
        if g["type"] != "csgo_match_game" or g["attributes"].get("isTotal"):
            continue
        rounds = sorted(g["attributes"].get("rounds") or [], key=lambda x: x["round"])
        score: dict[str, int] = {}
        for rd in rounds:
            name = teams.get(rd["winner_team"], rd["winner_team"])
            score[name] = score.get(name, 0) + 1
        last = rounds[-1] if rounds else None
        maps.append({
            "game_id": g["id"],
            "map_id": g["attributes"].get("mapId"),
            "rounds": len(rounds),
            "score": score,
            "ct": teams.get(last["ct"], last["ct"]) if last else None,
        })
    return {"status": 200, "maps": maps}


def best_prices(token_id: str) -> tuple[float | None, float | None]:
    book = client.get(f"{CLOB}/book", params={"token_id": token_id}).json()
    bids = [float(b["price"]) for b in book.get("bids", [])]
    asks = [float(a["price"]) for a in book.get("asks", [])]
    return (max(bids) if bids else None, min(asks) if asks else None)


def polymarket_state(slug: str) -> dict:
    e = client.get(f"{GAMMA}/events", params={"slug": slug}).json()[0]
    period = e.get("period") or ""
    current_map = period.split("/")[0] if "/" in period else None
    state = {"score": e.get("score"), "period": period, "live": e.get("live"), "ended": e.get("ended"), "prices": {}}
    for m in e["markets"]:
        title = m.get("groupItemTitle") or ""
        if m.get("sportsMarketType") == "moneyline" or title == f"Map {current_map} Winner":
            outcomes = json.loads(m["outcomes"])
            token = json.loads(m["clobTokenIds"])[0]
            bid, ask = best_prices(token)
            state["prices"][title] = {"outcome": outcomes[0], "bid": bid, "ask": ask}
    return state


slug = find_pm_event_slug()
print(f"{now()} polymarket slug: {slug} | writing to {out_path}", flush=True)
prev_cs = None
with out_path.open("a", encoding="utf-8") as f:
    while True:
        rec = {"t": dt.datetime.now(dt.timezone.utc).isoformat()}
        try:
            rec["cs"] = cybersport_state()
        except Exception as exc:
            rec["cs"] = {"error": repr(exc)}
        try:
            rec["pm"] = polymarket_state(slug)
        except Exception as exc:
            rec["pm"] = {"error": repr(exc)}
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()

        cs = rec["cs"]
        if cs.get("status") == 200:
            cs_txt = " ; ".join(f"map{m['map_id']} r{m['rounds']} {m['score']} CT={m['ct']}" for m in cs["maps"] if m["rounds"])
        else:
            cs_txt = f"cs={cs.get('status') or cs.get('error')}"
        changed = "*" if cs != prev_cs else " "
        prev_cs = cs
        pm = rec["pm"]
        pm_txt = (
            f"pm {pm.get('period')} {pm.get('score')} "
            + " ".join(f"[{k}: {v['outcome']} {v['bid']}/{v['ask']}]" for k, v in pm.get("prices", {}).items())
            if "error" not in pm
            else f"pm error {pm['error']}"
        )
        print(f"{now()}{changed} {cs_txt} | {pm_txt}", flush=True)
        if pm.get("ended"):
            break
        time.sleep(interval)
