"""What cybersport.ru, PandaScore and OpenDota give for Dota 2 (picks, players, teams, places, limits).

Raw responses are written to data/probe/dota_*.json for inspection.

  python scripts/research/dota_sources_probe.py
"""

import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv  # noqa: E402

from pmbot import storage  # noqa: E402
from pmbot.cybersport import API  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "data" / "probe"
OD = "https://api.opendota.com/api"


def dump(name: str, obj) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"dota_{name}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=1)[:3_000_000], encoding="utf-8")


def cybersport(http: HttpClient) -> None:
    doc = http.get_json(f"{API}/matches", {"filter[date]": "2026-10-04", "page[limit]": 100})
    disc = {x["id"]: x["attributes"].get("title") or x["attributes"].get("name") or x["attributes"].get("slug")
            for x in doc.get("included", []) if x["type"] == "discipline"}
    counts = Counter(x["relationships"]["discipline"]["data"]["id"] for x in doc["data"])
    print("cybersport disciplines on 2026-10-04:", {k: (disc.get(k), v) for k, v in counts.items()})
    dota_id = next((k for k, v in disc.items() if v and "dota" in str(v).lower()), None)
    print("  dota discipline id:", dota_id)
    if not dota_id:
        return
    doc = http.get_json(f"{API}/matches", {"filter[date]": "2026-10-04", "filter[disciplineIds]": dota_id,
                                            "page[limit]": 100})
    dump("cs_list", doc)
    finished = [m for m in doc["data"] if m["attributes"].get("isFinished")]
    print(f"  dota matches that day: {len(doc['data'])}, finished {len(finished)}")
    if not finished:
        return
    mid = finished[0]["id"]
    detail = http.get_json(f"{API}/matches/{mid}")
    dump("cs_match", detail)
    print("  match detail included types:", Counter(x["type"] for x in detail.get("included", [])))
    for path in ("dota2", "dota", "dota-2"):
        r = http.get(f"{API}/v1/match-stats/{path}", {"filter[internalId]": mid})
        print(f"  match-stats/{path}: {r.status_code} {len(r.content)} B")
        if r.status_code == 200:
            dump(f"cs_stats_{path}", r.json())
            print("    included types:", Counter(x["type"] for x in r.json().get("included", [])))


def pandascore(game_ids: list[int]) -> None:
    import httpx
    token = os.environ.get("PANDASCORE_ACCESS_TOKEN")
    if not token:
        print("no PANDASCORE_ACCESS_TOKEN")
        return
    c = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r = c.get("https://api.pandascore.co/dota2/matches", params={"filter[id]": ",".join(map(str, game_ids)),
                                                                   "per_page": 100})
    print(f"\nPandaScore /dota2/matches: {r.status_code}, found {len(r.json()) if r.is_success else '-'} of {len(game_ids)}"
          f", remaining {r.headers.get('x-rate-limit-remaining')}")
    if r.is_success and r.json():
        dump("ps_matches", r.json())
        m = r.json()[0]
        print("  match keys:", sorted(m))
        print("  game keys:", sorted((m.get("games") or [{}])[0]))
        print("  opponent keys:", sorted((m.get("opponents") or [{}])[0].get("opponent", {})))
    for path, params in (("dota2/teams", {"per_page": 5}), ("dota2/players", {"per_page": 5}),
                         ("dota2/heroes", {"per_page": 5}), ("dota2/tournaments", {"per_page": 2}),
                         ("dota2/series", {"per_page": 2}), ("dota2/leagues", {"per_page": 2})):
        r = c.get(f"https://api.pandascore.co/{path}", params=params)
        print(f"  /{path}: {r.status_code}", sorted(r.json()[0]) if r.is_success and r.json() else r.text[:120])
        if r.is_success:
            dump("ps_" + path.split("/")[1], r.json())
    if r.is_success and game_ids:
        g = c.get(f"https://api.pandascore.co/dota2/matches/{game_ids[0]}")
        print(f"  /dota2/matches/{{id}}: {g.status_code}")
        gg = c.get("https://api.pandascore.co/dota2/games", params={"per_page": 2})
        print(f"  /dota2/games: {gg.status_code} {gg.text[:200]}")


def opendota(http: HttpClient) -> None:
    r = http.get(f"{OD}/proMatches")
    print(f"\nOpenDota /proMatches: {r.status_code}, limits: "
          f"{ {k: v for k, v in r.headers.items() if 'rate' in k.lower()} }")
    pro = r.json()
    dump("od_promatches", pro[:5])
    print("  row keys:", sorted(pro[0]))
    m = http.get_json(f"{OD}/matches/{pro[0]['match_id']}")
    dump("od_match", m)
    print("  match keys:", sorted(m))
    print("  player keys:", sorted(m["players"][0])[:80])
    print("  picks_bans:", (m.get("picks_bans") or [])[:3])
    for path in ("teams", "leagues", "heroes", "proPlayers"):
        r = http.get(f"{OD}/{path}")
        rows = r.json() if r.is_success else []
        print(f"  /{path}: {r.status_code}, {len(rows)} rows, keys {sorted(rows[0]) if rows else ''}")
    sql = ("select m.match_id, m.start_time, m.duration, m.radiant_win, m.leagueid, m.radiant_team_id, m.dire_team_id, "
           "array_length(m.radiant_gold_adv, 1) gold_points from matches m order by m.match_id desc limit 3")
    r = http.get(f"{OD}/explorer", {"sql": sql})
    print(f"  /explorer: {r.status_code} {r.text[:400]}")


def main() -> None:
    load_dotenv()
    http = HttpClient(min_interval=1.1)
    conn = sqlite3.connect(storage.db_path("dota2"))
    ids = [r[0] for r in conn.execute(
        "SELECT game_id FROM pm_events WHERE game_id IS NOT NULL ORDER BY start_time DESC LIMIT 20")]
    print("Polymarket dota game ids:", ids[:5], "events with gameId:",
          conn.execute("SELECT count(game_id), count(*) FROM pm_events").fetchone())
    cybersport(http)
    pandascore(ids)
    opendota(http)


if __name__ == "__main__":
    main()
