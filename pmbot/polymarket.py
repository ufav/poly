import datetime as dt
import json
import re
import sqlite3
from collections.abc import Iterator

from pmbot.http import HttpClient

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
CS2_SERIES_ID = 10310
DOTA2_SERIES_ID = 10309
SERIES = {"cs2": CS2_SERIES_ID, "dota2": DOTA2_SERIES_ID}
# tennis singles: ATP (with Challengers), WTA, ITF (since 05.2026); doubles have no volume
TENNIS_SERIES = {"atp": 10365, "wta": 10366, "itf": 11634}
# Gamma /sports: sport codes vb*; series ids for indoor volleyball leagues + VNL / Euro / Worlds
VOLLEYBALL_SERIES = {
    "vbsuperlega": 11920, "vbplusliga": 11921, "vbefeler": 11922, "vbsuperliga": 11923,
    "vbliguea": 11924, "vbbundesliga": 11925, "vbsvleague": 11926, "vbvleague": 11927,
    "vbvnl": 11928, "vbcl": 11929, "vbworlds": 11930, "vbeuro": 11931,
    "vbligueb": 12585, "vbligueaw": 12586, "vbbundesligaw": 12587, "vb2bundesliga": 12588,
    "vb2bundesligaw": 12589, "vbbelligaa": 12590, "vbdivisao1": 12591,
}
# Fat non-esports / non-tennis (sample vol of recent closed pages; collect separately)
NFL_SERIES = {"nfl": 12185}
NBA_SERIES = {"nba": 10345}
NHL_SERIES = {"nhl": 10346}
LOL_SERIES = {"lol": 10311}
UFC_SERIES = {"ufc": 38}
FIFA_SERIES = {"fifwc": 11433}  # World Cup match markets
SOCCER_SERIES = {
    "epl": 10188, "ucl": 10204, "uel": 10209, "lal": 10193, "bun": 10194, "sea": 10203,
    "fl1": 10195, "efl": 10329, "dfb": 10317, "itc": 10287, "por": 10330, "spl": 10361,
    "uef": 10243, "acn": 10786, "unl": 11446, "lib": 10289,
}
SPORT_SERIES = {
    **{k: (v,) for k, v in SERIES.items()},
    "tennis": tuple(TENNIS_SERIES.values()),
    "volleyball": tuple(VOLLEYBALL_SERIES.values()),
    "nfl": tuple(NFL_SERIES.values()),
    "nba": tuple(NBA_SERIES.values()),
    "nhl": tuple(NHL_SERIES.values()),
    "lol": tuple(LOL_SERIES.values()),
    "ufc": tuple(UFC_SERIES.values()),
    "fifa": tuple(FIFA_SERIES.values()),
    "soccer": tuple(SOCCER_SERIES.values()),
}
FAT_SPORTS = ("nfl", "nba", "nhl", "lol", "ufc", "fifa", "soccer")
TRADES_PAGE = 500
HISTORY_MARKET_TYPES = {"moneyline", "child_moneyline"}
HISTORY_BEFORE_START = dt.timedelta(hours=6)
HISTORY_AFTER_START = dt.timedelta(hours=12)


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T", 1))


def iter_closed_events(http: HttpClient, created_from: dt.datetime, created_to: dt.datetime,
                       step: dt.timedelta = dt.timedelta(days=7), series_id: int = CS2_SERIES_ID) -> Iterator[dict]:
    """Closed events of a series (CS2 by default), walking start-date windows (Gamma offset pagination fails on
    deep pages)."""
    seen: set[str] = set()
    window_start = created_from
    while window_start < created_to:
        window_end = min(window_start + step, created_to)
        offset = 0
        while True:
            page = http.get_json(f"{GAMMA}/events", {
                "series_id": series_id,
                "closed": "true",
                "limit": 100,
                "offset": offset,
                "start_date_min": window_start.isoformat().replace("+00:00", "Z"),
                "start_date_max": window_end.isoformat().replace("+00:00", "Z"),
            })
            for event in page:
                if event["id"] not in seen:
                    seen.add(event["id"])
                    yield event
            if len(page) < 100:
                break
            offset += 100
        window_start = window_end


def _title_parts(title: str) -> tuple[int | None, str | None]:
    best_of = re.search(r"\(BO(\d)\)", title, re.I)
    tournament = title.split(" - ", 1)[1].strip() if " - " in title else None
    return (int(best_of.group(1)) if best_of else None, tournament)


def _classify_market(m: dict) -> str | None:
    """Gamma sportsMarketType, or infer 2-way match / binary team-win (LoL, UFC, EPL Yes/No)."""
    t = m.get("sportsMarketType")
    if t:
        return t
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if len(outcomes) != 2:
        return None
    label = f"{m.get('groupItemTitle') or ''} {m.get('question') or ''}"
    if outcomes[0] in ("Yes", "No"):
        if re.search(r"\bdraw\b", label, re.I):
            return "draw"
        return "moneyline"  # Will <team> win?
    return "moneyline"  # two named sides


def save_event(conn: sqlite3.Connection, event: dict) -> None:
    best_of, tournament = _title_parts(event["title"])
    tournament = tournament or (event.get("eventMetadata") or {}).get("league")  # tennis: 'US Open ATP: A vs B'
    markets = event.get("markets") or []
    moneyline = next((m for m in markets if _classify_market(m) == "moneyline"), None)
    if moneyline:
        outcomes = json.loads(moneyline.get("outcomes") or "[null, null]")
        if outcomes[0] in ("Yes", "No"):
            # binary team-win: team from groupItemTitle; counterpart unknown here
            teams = [moneyline.get("groupItemTitle"), None]
        else:
            teams = outcomes
    else:
        teams = [None, None]
    raw = {k: v for k, v in event.items() if k != "markets"}
    conn.execute(
        """INSERT INTO pm_events (id, slug, title, game_id, start_time, best_of, tournament, team_a, team_b,
                                  score, volume, liquidity, closed, raw, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET score=excluded.score, volume=excluded.volume,
               liquidity=excluded.liquidity, closed=excluded.closed, raw=excluded.raw, fetched_at=excluded.fetched_at""",
        (event["id"], event.get("slug"), event["title"], event.get("gameId"), event.get("startTime"), best_of,
         tournament, teams[0], teams[1], event.get("score"), float(event.get("volume") or 0),
         float(event.get("liquidity") or 0), int(bool(event.get("closed"))), json.dumps(raw, ensure_ascii=False),
         dt.datetime.now(dt.timezone.utc).isoformat()),
    )
    for m in markets:
        title = m.get("groupItemTitle") or ""
        map_no = re.match(r"(?:Map|Game) (\d+)", title) or re.search(r"\bSet (\d+) Winner$", title)
        outcomes = json.loads(m.get("outcomes") or "[null, null]")
        tokens = json.loads(m.get("clobTokenIds") or "[null, null]")
        prices = json.loads(m.get("outcomePrices") or "[null, null]")
        mtype = _classify_market(m)
        needs_history = mtype in HISTORY_MARKET_TYPES
        conn.execute(
            """INSERT INTO pm_markets (id, event_id, condition_id, market_type, group_title, map_number,
                                       outcome_a, outcome_b, token_a, token_b, final_price_a, volume, closed,
                                       history_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET final_price_a=excluded.final_price_a, volume=excluded.volume,
                   closed=excluded.closed, market_type=COALESCE(excluded.market_type, pm_markets.market_type)""",
            (m["id"], event["id"], m.get("conditionId"), mtype, title,
             int(map_no.group(1)) if map_no else None, outcomes[0], outcomes[1], tokens[0], tokens[1],
             float(prices[0]) if prices[0] is not None else None, float(m.get("volume") or 0),
             int(bool(m.get("closed"))), "pending" if needs_history else "skipped"),
        )


def fetch_price_history(http: HttpClient, token_id: str, start: dt.datetime) -> list[dict]:
    r = http.get(f"{CLOB}/prices-history", {
        "market": token_id,
        "startTs": int((start - HISTORY_BEFORE_START).timestamp()),
        "endTs": int((start + HISTORY_AFTER_START).timestamp()),
        "fidelity": 1,
    })
    if r.status_code != 200:
        return []
    return r.json().get("history", [])


def collect_pending_histories(http: HttpClient, conn: sqlite3.Connection, limit: int | None = None,
                              min_volume: float = 0) -> int:
    """Minute histories of pending markets with volume >= min_volume (smaller ones stay pending)."""
    rows = conn.execute(
        """SELECT m.id, m.token_a, e.start_time FROM pm_markets m JOIN pm_events e ON e.id = m.event_id
           WHERE m.history_status = 'pending' AND m.token_a IS NOT NULL AND e.start_time IS NOT NULL
             AND m.volume >= ?
           ORDER BY e.start_time""" + (f" LIMIT {int(limit)}" if limit else ""), (min_volume,)
    ).fetchall()
    for i, (market_id, token, start_time) in enumerate(rows, 1):
        history = fetch_price_history(http, token, parse_ts(start_time))
        conn.executemany("INSERT OR IGNORE INTO pm_prices (token_id, t, p) VALUES (?,?,?)",
                         [(token, h["t"], h["p"]) for h in history])
        conn.execute("UPDATE pm_markets SET history_status=? WHERE id=?",
                     ("done" if history else "empty", market_id))
        if i % 50 == 0:
            conn.commit()
            print(f"  histories {i}/{len(rows)}", flush=True)
    conn.commit()
    return len(rows)


def fetch_trades(http: HttpClient, condition_id: str, max_pages: int = 20) -> list[dict] | None:
    """Taker trades of a market (newest first in the API); None on error. side/price are for `asset`."""
    out = []
    for page in range(max_pages):
        r = http.get(f"{DATA}/trades", {"market": condition_id, "limit": TRADES_PAGE,
                                        "offset": page * TRADES_PAGE, "takerOnly": "true"})
        if r.status_code != 200:
            return out or None
        batch = r.json()
        out += batch
        if len(batch) < TRADES_PAGE:
            break
    return out


def save_trades(conn: sqlite3.Connection, market_id: str, trades: list[dict] | None) -> None:
    with conn:
        conn.executemany(
            """INSERT OR IGNORE INTO pm_trades (condition_id, asset, t, side, price, size, tx)
               VALUES (?,?,?,?,?,?,?)""",
            [(x["conditionId"], x["asset"], x["timestamp"], x["side"], x["price"], x["size"], x.get("transactionHash"))
             for x in trades or []])
        conn.execute("UPDATE pm_markets SET trades_status=? WHERE id=?",
                     ("error" if trades is None else "done" if trades else "empty", market_id))
