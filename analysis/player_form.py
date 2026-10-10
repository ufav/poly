"""Star player form ("кураж лидера"): does the team leader's form matter beyond the market price?

Per-map performance = ADR (match-stats `rating` is the player's overall rating at fetch time, not per map).
Leader = the team's player with the best average ADR over the team's previous maps (>= 5 maps, before the match).
1. Pre-match: leader's last 5 maps vs his previous baseline -> match result vs Polymarket match price at start.
2. Between maps: leader's ADR on map N of this match -> map N+1 result vs the map N+1 price at its start
   (PandaScore begin_at; cybersport publishes player stats after the map, so this is tradable).
   Split by whether the team won map N (the market sees the map score, not who carried).

  python analysis/player_form.py [--slippage 0.01]
"""

import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmbot import storage  # noqa: E402
from pmbot.join import norm_team  # noqa: E402
from pmbot.maptiming import price_at, price_series  # noqa: E402
from halftime_price import ps_windows  # noqa: E402

BASELINE_MAPS = 30
FORM_MAPS = 5


def team_of(conn) -> dict:
    """player_id -> [(team_id, since, till)]"""
    out = defaultdict(list)
    for pid, tid, since, till in conn.execute("SELECT player_id, team_id, since, till FROM cs_team_players"):
        out[str(pid)].append((str(tid), since or "0000", till or "9999"))
    return out


def load_maps(conn) -> list[dict]:
    """Maps with player stats split into team1/team2 (cs_matches order), chronological."""
    rosters = team_of(conn)
    stats = defaultdict(list)
    # `rating` in match-stats is the player's overall rating at fetch time (not per map) -> use ADR
    for gid, pid, adr in conn.execute("SELECT game_id, player_id, adr FROM cs_player_stats WHERE adr > 0"):
        stats[gid].append((pid, adr))
    maps = []
    for gid, mid, number, s1, s2, date, t1, t2, st1, st2, ext in conn.execute(
            """SELECT g.id, g.match_id, g.number, g.score1, g.score2, m.date, m.team1_id, m.team2_id,
                      m.stats_team1, m.stats_team2, m.external_id
               FROM cs_games g JOIN cs_matches m ON m.id = g.match_id
               WHERE m.team1_id IS NOT NULL ORDER BY m.date, m.time, g.match_id, g.number"""):
        players = stats.get(gid)
        if not players or s1 is None or s2 is None or s1 == s2:
            continue
        side = {"1": [], "2": [], None: []}
        for pid, rating in players:
            teams = {t for t, since, till in rosters.get(pid, []) if since <= date <= till}
            k = "1" if str(t1) in teams else "2" if str(t2) in teams else None
            side[k].append((pid, rating))
        if len(side["1"]) == 5:
            side["2"] += side[None]
        elif len(side["2"]) == 5:
            side["1"] += side[None]
        if len(side["1"]) != 5 or len(side["2"]) != 5:
            continue
        maps.append({"game": gid, "match": mid, "number": number, "date": date, "ext": ext,
                     "team": {"1": str(t1), "2": str(t2)}, "name": {"1": st1, "2": st2},
                     "players": {"1": dict(side["1"]), "2": dict(side["2"])}, "winner": "1" if s1 > s2 else "2"})
    return maps


def leaders_and_form(maps: list[dict]) -> None:
    """Adds per map and side: leader id, his pre-match form delta, using only earlier matches of that team."""
    history = defaultdict(list)  # team_id -> [(match_id, {player: rating})]
    by_match = defaultdict(list)
    for m in maps:
        by_match[m["match"]].append(m)
    for mid, ms in by_match.items():
        for side in ("1", "2"):
            tid = ms[0]["team"][side]
            past = history[tid][-BASELINE_MAPS:]
            per_player = defaultdict(list)
            for _, ratings in past:
                for p, r in ratings.items():
                    per_player[p].append(r)
            current = set(ms[0]["players"][side])
            cands = {p: v for p, v in per_player.items() if p in current and len(v) >= 5}
            leader = max(cands, key=lambda p: sum(cands[p]) / len(cands[p])) if cands else None
            form = None
            if leader and len(cands[leader]) >= FORM_MAPS + 5:
                recent, base = cands[leader][-FORM_MAPS:], cands[leader][:-FORM_MAPS]
                form = sum(recent) / len(recent) - sum(base) / len(base)
            for m in ms:
                m.setdefault("leader", {})[side] = leader
                m.setdefault("form", {})[side] = form
            for m in ms:
                history[tid].append((mid, m["players"][side]))


def market_tokens(conn, game_id) -> tuple[dict, tuple | None, str | None, int | None]:
    ev = conn.execute("SELECT id, start_time, best_of FROM pm_events WHERE game_id = ?", (game_id,)).fetchone()
    if not ev:
        return {}, None, None, None
    maps, ml = {}, None
    for mtype, num, token, outcome_a in conn.execute(
            "SELECT market_type, map_number, token_a, outcome_a FROM pm_markets WHERE event_id = ? AND history_status = 'done'",
            (ev[0],)):
        if mtype == "child_moneyline" and num:
            maps[num] = (token, outcome_a)
        elif mtype == "moneyline":
            ml = (token, outcome_a)
    return maps, ml, ev[1], ev[2]


def report(label: str, items: list[tuple[float, bool]], slippage: float) -> None:
    if len(items) < 30:
        print(f"   {label:44} n={len(items):4d}  (too few)")
        return
    n = len(items)
    win = sum(w for _, w in items) / n
    price = sum(p for p, _ in items) / n
    cost = sum(p + slippage + 0.05 * p * (1 - p) for p, _ in items) / n
    se2 = 2 * (win * (1 - win) / n) ** 0.5
    print(f"   {label:44} n={n:4d}  price {price:5.3f}  win {win:6.1%} ±{se2:5.1%}  edge {win - price:+.3f}"
          f"  ROI {(win - cost) / cost:+6.1%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slippage", type=float, default=0.01)
    args = ap.parse_args()
    conn = storage.connect()
    maps = load_maps(conn)
    leaders_and_form(maps)
    print(f"maps with 5v5 player stats: {len(maps)}; matches {len({m['match'] for m in maps})}\n")

    by_match = defaultdict(list)
    for m in maps:
        by_match[m["match"]].append(m)

    pre = defaultdict(list)
    between = defaultdict(list)
    for mid, ms in by_match.items():
        ms.sort(key=lambda m: m["number"])
        ext = ms[0]["ext"]
        if not ext:
            continue
        map_tokens, ml, start_time, best_of = market_tokens(conn, ext)
        if not ml or not start_time:
            continue
        ml_series = price_series(conn, ml[0])
        start_ts = int(dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
        p0 = price_at(ml_series, start_ts)
        a_side = next((s for s in ("1", "2") if norm_team(ms[0]["name"][s] or "") == norm_team(ml[1])), None)
        if p0 is None or not a_side:
            continue
        match_winner = max(("1", "2"), key=lambda s: sum(m["winner"] == s for m in ms))
        for side in ("1", "2"):
            form = ms[0]["form"][side]
            if form is None:
                continue
            p = p0 if side == a_side else 1 - p0
            b = "leader hot (ADR +10)" if form >= 10 else "leader cold (ADR -10)" if form <= -10 else "leader normal"
            pre[b].append((p, match_winner == side))
            pre["all"].append((p, match_winner == side))

        windows = ps_windows(conn, ext)
        wins = {"1": 0, "2": 0}
        for prev, nxt in zip(ms, ms[1:]):
            wins[prev["winner"]] += 1
            if nxt["number"] != prev["number"] + 1 or nxt["number"] not in windows:
                continue
            deciding = best_of and wins["1"] == wins["2"] == (best_of - 1) // 2
            market = map_tokens.get(nxt["number"]) or (ml if deciding else None)
            if not market:
                continue
            series = ml_series if market is ml else price_series(conn, market[0])
            pa = price_at(series, windows[nxt["number"]][0])
            m_side = next((s for s in ("1", "2") if norm_team(nxt["name"][s] or "") == norm_team(market[1])), None)
            if pa is None or not m_side:
                continue
            for side in ("1", "2"):
                leader = prev["leader"][side]
                if not leader or leader not in prev["players"][side]:
                    continue
                r = prev["players"][side][leader]
                p = pa if side == m_side else 1 - pa
                won_prev = prev["winner"] == side
                b = "leader ADR >= 100" if r >= 100 else "leader ADR < 65" if r < 65 else "leader ADR 65-100"
                item = (p, nxt["winner"] == side)
                between[("won" if won_prev else "lost", b)].append(item)
                between[("all", b)].append(item)

    print("1. Pre-match: leader's last 5 maps vs his baseline -> match result vs match price at start")
    for b in ("all", "leader hot (ADR +10)", "leader normal", "leader cold (ADR -10)"):
        report(b, pre[b], args.slippage)

    print("\n2. Between maps: leader's ADR on the previous map -> next map vs its price at map start")
    for res in ("all", "won", "lost"):
        for b in ("leader ADR >= 100", "leader ADR 65-100", "leader ADR < 65"):
            report(f"team {res} prev map, {b}", between[(res, b)], args.slippage)


if __name__ == "__main__":
    main()
