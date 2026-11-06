"""Polymarket sports audit (plan stage 1): every series of Gamma /sports, events that started in the last --days -
count, traded volume (total, median and p90 per event), liquidity now, market types of a match - grouped into our
sports (football, tennis, basketball, hockey, volleyball, other). Writes a CSV of all series.

  python analysis/pm_sports_audit.py [--days 90] [--out data/pm_sports_audit.csv]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot.http import HttpClient  # noqa: E402
from pmbot.polymarket import GAMMA  # noqa: E402

# Gamma sport code -> our sport (by code / name keywords; unknown -> other)
FOOTBALL = ("epl", "lal", "bun", "sea", "fl1", "ucl", "uel", "ucol", "mls", "ere", "por", "tur", "spl", "bra",
            "arg", "mex", "jap", "kor", "chi", "aus", "efl", "elc", "fac", "cdr", "dfb", "itc", "cof", "lib", "sud",
            "soccer", "fifwc", "uef", "conmebol", "concacaf", "afc", "caf", "nwsl", "wsl", "rus", "den", "nor", "swe",
            "sco", "bel", "gre", "aut", "sui", "pol", "rou", "ukr", "cze", "cro", "srb", "isr", "ksa", "uae", "qat",
            "egy", "mar", "ind", "col", "per", "uru", "ecu", "par", "chl", "bol", "ven", "usl", "can", "cpl")
KEYWORDS = [
    ("tennis", ("atp", "wta", "itf", "tennis", "challenger", "wimbledon", "us open", "roland", "australian open")),
    ("basketball", ("nba", "wnba", "ncaab", "cbb", "basket", "euroleague", "acb", "nbl", "cba", "bbl", "kbl",
                    "fiba", "eurocup", "vtb")),
    ("hockey", ("nhl", "khl", "hockey", "shl", "liiga", "del", "ahl", "nla", "iihf")),
    ("volleyball", ("volley",)),
    ("football", ("soccer", "football club", "premier league", "la liga", "serie a", "bundesliga", "ligue 1",
                  "champions league", "europa league", "conference league", "mls", "eredivisie", "liga", "copa",
                  "cup", "fifa", "uefa", "world cup")),
]
NOT_OURS = ("nfl", "ncaaf", "cfb", "mlb", "kbo", "npb", "ufc", "mma", "boxing", "f1", "nascar", "golf", "pga",
            "cricket", "ipl", "lol", "cs2", "dota", "valorant", "rugby", "afl", "darts", "snooker", "ufl", "esports")


def our_sport(code: str, name: str) -> str:
    c, n = code.lower(), name.lower()
    if any(k == c or c.startswith(k) for k in NOT_OURS) or any(k in n for k in ("nfl", "mlb", "ufc", "cricket",
                                                                               "league of legends", "counter")):
        return "other"
    for sport, words in KEYWORDS:
        if any(w in c or w in n for w in words):
            return sport
    if c in FOOTBALL or c[:3] in FOOTBALL:
        return "football"
    return "other"


def events_of(http: HttpClient, series: str, since: dt.datetime) -> list[dict]:
    out = []
    for closed in ("true", "false"):
        offset = 0
        while True:
            page = http.get_json(f"{GAMMA}/events", {"series_id": series, "closed": closed, "limit": 100,
                                                     "offset": offset,
                                                     "start_date_min": since.strftime("%Y-%m-%dT%H:%M:%SZ")})
            out += page
            if len(page) < 100:
                break
            offset += 100
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "data" / "pm_sports_audit.csv"))
    args = ap.parse_args()
    http = HttpClient(min_interval=0.25)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    leagues = http.get_json(f"{GAMMA}/sports")
    rows = []
    for n, lg in enumerate(leagues, 1):
        series = str(lg.get("series") or "").split(",")[0].strip()
        if not series:
            continue
        sport = our_sport(lg.get("sport") or "", lg.get("name") or "")
        try:
            evs = events_of(http, series, since)
        except Exception as e:  # one bad series must not stop the audit
            print(f"  {lg.get('sport')}: {e!r:.80}", flush=True)
            continue
        # match events only (two-sided games), not futures / awards
        games = [e for e in evs if any(m.get("sportsMarketType") in ("moneyline", "child_moneyline")
                                       for m in e.get("markets") or [])]
        vols = [float(e.get("volume") or 0) for e in games]
        liq = [float(e.get("liquidity") or 0) for e in games if not e.get("closed")]
        kinds = Counter(m.get("sportsMarketType") or "other" for e in games for m in e.get("markets") or [])
        rows.append({"sport": sport, "code": lg.get("sport"), "name": lg.get("name"), "series": series,
                     "events": len(games), "volume": round(sum(vols)), "median_vol": round(statistics.median(vols))
                     if vols else 0, "p90_vol": round(sorted(vols)[int(0.9 * (len(vols) - 1))]) if vols else 0,
                     "open_liquidity": round(statistics.mean(liq)) if liq else 0,
                     "market_types": ";".join(f"{k}:{v}" for k, v in kinds.most_common(6))})
        if n % 25 == 0:
            print(f"[{n}/{len(leagues)}] series audited", flush=True)
    rows.sort(key=lambda r: (r["sport"], -r["volume"]))
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    by = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        b = by[r["sport"]]
        b[0] += 1 if r["events"] else 0
        b[1] += r["events"]
        b[2] += r["volume"]
    print(f"\nlast {args.days} days, match events (moneyline):")
    for sport, (series_n, ev, vol) in sorted(by.items(), key=lambda x: -x[1][2]):
        print(f"  {sport:<11} series with games {series_n:4d}  events {ev:6d}  volume ${vol / 1e6:9.1f}M")
    for sport in ("football", "tennis", "basketball", "hockey", "volleyball"):
        top = [r for r in rows if r["sport"] == sport and r["events"]][:12]
        print(f"\n{sport}: top series by volume")
        for r in top:
            print(f"  {r['code']:<10} {r['name'][:34]:<34} ev {r['events']:5d}  vol ${r['volume'] / 1e6:7.2f}M  "
                  f"median ${r['median_vol']:>9,}  p90 ${r['p90_vol']:>10,}  liq ${r['open_liquidity']:>8,}  "
                  f"{r['market_types'][:60]}")
    print(f"\nall series -> {args.out}")


if __name__ == "__main__":
    main()
