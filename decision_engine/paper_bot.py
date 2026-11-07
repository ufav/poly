"""Paper betting bot (docs/md1.md 9-11; the user's two bet kinds, memory: betting-goal). Every run (hourly, in
workers/pipeline.py):

1. settle the open paper bets whose matches have a result;
2. predictions for the Polymarket tennis matches starting in the next 24 h: A (CatBoost), B (form), C (combined),
   both sides, at the buy price (ask) of the latest order book snapshot -> predictions (append-only: a new version
   only when p or the price moved by 0.01+);
3. favourites shortlist: A p >= FAV_P and >= the ask, starting within NEWS_HOURS -> news check (Tavily + LLM,
   ai_engine.news_check); a confirmed injury / withdrawal / suspension / fatigue of the favourite drops it;
4. bets (flat STAKE): favourites parlays - 2 legs of different matches, total odds >= PARLAY_MIN_ODDS, the highest
   joint probability first, up to MAX_PARLAYS a day; value singles (experimental) - A p >= VALUE_MIN_P and
   p * odds - 1 >= VALUE_EDGE, up to MAX_SINGLES a day;
5. Telegram: the new bets and the settled ones.

  python -m decision_engine.paper_bot              # one cycle
  python -m decision_engine.paper_bot --report     # results by approach / bet kind
  python -m decision_engine.paper_bot --dry        # no news / bets / messages, predictions only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
from sqlalchemy import insert, text

from core.config import settings
from core.db import engine, session
from core.models import PaperBet, Prediction

ALMATY = ZoneInfo("Asia/Almaty")
HORIZON_H = 24
NEWS_HOURS = 12
FAV_P = 0.75           # 0.80 rarely pairs to >= 1.80 (favourites at 0.80+ cost 1.15-1.33)
PARLAY_MIN_ODDS = 1.80
MAX_PARLAYS = 3
VALUE_EDGE = 0.05
VALUE_MIN_P = 0.45
MAX_SINGLES = 3
STAKE = Decimal("30")          # ₸ of the 300 ₸ playing limit (docs/md1.md 10, flat stake)
ASK_MAX_AGE_MIN = 40
BAD_NEWS = {"injury", "withdrawal", "suspension", "fatigue"}
# reassurance the LLM may still list as a fact: "not listed among injured players", "no injury reported"
NEGATED = re.compile(r"\b(no|not|without|fully fit|cleared)\b.{0,40}\b(injur|listed|reported|concern|issue|problem)",
                     re.IGNORECASE)


# ---------- data ----------

def upcoming(c) -> pd.DataFrame:
    return pd.read_sql(text("""
        SELECT m.id match_id, m.start_time, th.name home, ta.name away, l.name league, mk.id market_id,
               (SELECT se.external_name FROM source_entities se WHERE se.source_id = 4 AND se.entity_type = 'team'
                  AND se.entity_id = m.home_team_id LIMIT 1) pm_home,
               (SELECT se.external_name FROM source_entities se WHERE se.source_id = 4 AND se.entity_type = 'team'
                  AND se.entity_id = m.away_team_id LIMIT 1) pm_away
        FROM matches m JOIN teams th ON th.id = m.home_team_id JOIN teams ta ON ta.id = m.away_team_id
        JOIN leagues l ON l.id = m.league_id
        JOIN markets mk ON mk.match_id = m.id AND mk.kind = 'moneyline' AND mk.extra ? 'tokens'
        WHERE m.sport_id = 2 AND m.status = 'scheduled'
          AND m.start_time BETWEEN now() + interval '15 minutes' AND now() + make_interval(hours => :h)"""),
        c, params={"h": HORIZON_H})


def asks(c, market_ids: list[int]) -> dict[tuple[int, str], tuple[float, float | None]]:
    rows = c.execute(text("""
        SELECT DISTINCT ON (market_id, outcome) market_id, outcome, ask, ask_size FROM odds_history
        WHERE source_id = 4 AND snapshot IS NULL AND market_id = ANY(:ids) AND ask IS NOT NULL
          AND taken_at > now() - make_interval(mins => :age)
        ORDER BY market_id, outcome, taken_at DESC"""), {"ids": market_ids, "age": ASK_MAX_AGE_MIN}).all()
    return {(mk, o): (float(a), float(z) if z is not None else None) for mk, o, a, z in rows}


def last_versions(c, match_ids: list[int]) -> dict[tuple[int, str, str], tuple[float, float | None]]:
    rows = c.execute(text("""
        SELECT DISTINCT ON (match_id, approach, outcome) match_id, approach, outcome, p, price FROM predictions
        WHERE match_id = ANY(:ids) ORDER BY match_id, approach, outcome, created_at DESC"""),
        {"ids": match_ids}).all()
    return {(m, a, o): (float(p), float(pr) if pr is not None else None) for m, a, o, p, pr in rows}


# ---------- predictions ----------

def predict_cycle(dry: bool) -> pd.DataFrame:
    from ml_engine.tennis import live
    with engine().connect() as c:
        up = upcoming(c)
        if up.empty:
            return up
        ask = asks(c, up.market_id.astype(int).tolist())
        prev = last_versions(c, up.match_id.astype(int).tolist())
    pr = live.predict(up.match_id.astype(int).tolist())
    df = up.merge(pr, on="match_id", how="inner")
    rows, out = [], []
    for r in df.itertuples():
        for side in ("home", "away"):
            a = ask.get((int(r.market_id), side))
            price = a[0] if a else None
            for approach, ph in (("A", r.p_a), ("B", r.p_b), ("C", r.p_c)):
                p = float(ph if side == "home" else 1 - ph)
                odds = 1 / price if price else None
                ev = p * odds - 1 if odds else None
                out.append({"match_id": r.match_id, "market_id": r.market_id, "side": side, "approach": approach,
                            "p": p, "price": price, "odds": odds, "ev": ev, "ask_size": a[1] if a else None,
                            "start_time": r.start_time, "home": r.home, "away": r.away, "league": r.league,
                            "pm_home": r.pm_home, "pm_away": r.pm_away})
                old = prev.get((int(r.match_id), approach, side))
                if old and abs(old[0] - p) < 0.01 and (old[1] is None) == (price is None) and \
                        (price is None or abs(old[1] - price) < 0.01):
                    continue
                rows.append({"match_id": int(r.match_id), "market_id": int(r.market_id), "outcome": side,
                             "approach": approach, "model": f"{live.VERSION}_{approach.lower()}",
                             "p": round(p, 5), "price": round(price, 5) if price else None,
                             "odds": round(odds, 3) if odds else None, "ev": round(ev, 4) if ev is not None else None,
                             "extra": {"ask_size": a[1] if a else None, "n_h": int(r.n_h), "n_a": int(r.n_a)}})
    if rows and not dry:
        with session() as s:
            s.execute(insert(Prediction), rows)
    print(f"predictions: {len(df)} matches, {len(rows)} new versions", flush=True)
    return pd.DataFrame(out)


# ---------- news ----------

def surname(name: str | None) -> str:
    """'Shelton Ben' / 'Ben Shelton' -> the longest word, lower case (good enough to spot the player in a fact)."""
    words = [w for w in (name or "").replace(".", " ").split() if len(w) > 2]
    return max(words, key=len).lower() if words else ""


def news_verdict(fav: dict) -> tuple[bool, dict]:
    """(ok, news result) for a shortlisted favourite; cached per match for NEWS_HOURS."""
    from ai_engine.news_check import check
    with engine().connect() as c:
        cached = c.execute(text("""SELECT news FROM predictions WHERE match_id = :m AND news IS NOT NULL
            AND created_at > now() - make_interval(hours => :h) ORDER BY created_at DESC LIMIT 1"""),
            {"m": int(fav["match_id"]), "h": NEWS_HOURS}).scalar()
    if cached:
        news = cached
    else:
        home = fav["pm_home"] or fav["home"]
        away = fav["pm_away"] or fav["away"]
        try:
            news = check(home, away, "tennis", fav["league"], f"{fav['start_time']:%Y-%m-%d}")
        except (httpx.HTTPError, RuntimeError, ValueError) as e:  # no news = no confirmation: keep, note why
            news = {"facts": [], "error": repr(e)[:200], "red_flags": [], "summary": ""}
    me = {surname(fav["home" if fav["side"] == "home" else "away"]),
          surname(fav["pm_home" if fav["side"] == "home" else "pm_away"])} - {""}
    bad = [f for f in news.get("facts") or []
           if f.get("category") in BAD_NEWS and f.get("confirmed") and float(f.get("importance") or 0) >= 0.5
           and any(x in f"{f.get('subject', '')} {f.get('team', '')}".lower() for x in me)
           and not NEGATED.search(f.get("description") or "")]
    news["verdict"] = "drop" if bad else "ok"
    news["bad_facts"] = bad
    return not bad, news


# ---------- bets ----------

def today_count(c, kind: str) -> int:
    day0 = dt.datetime.now(ALMATY).replace(hour=0, minute=0, second=0, microsecond=0)
    return c.execute(text("SELECT count(*) FROM paper_bets WHERE kind = :k AND created_at >= :d"),
                     {"k": kind, "d": day0}).scalar()


def used_matches(c) -> set[int]:
    rows = c.execute(text("""SELECT (leg->>'match_id')::int FROM paper_bets, jsonb_array_elements(legs) leg
        WHERE status = 'open'""")).scalars().all()
    return set(rows)


def place(kind: str, approach: str, legs: list[dict]) -> int:
    odds = 1.0
    p = 1.0
    for leg in legs:
        odds *= leg["odds"]
        p *= leg["p"]
    with session() as s:
        return s.scalar(insert(PaperBet).values(kind=kind, approach=approach, legs=legs, odds=round(odds, 3),
                                                p=round(p, 5), stake=STAKE, status="open").returning(PaperBet.id))


def leg_of(x: dict) -> dict:
    pick = x["home"] if x["side"] == "home" else x["away"]
    return {"match_id": int(x["match_id"]), "market_id": int(x["market_id"]), "outcome": x["side"],
            "pick": pick, "match": f"{x['home']} — {x['away']}", "league": x["league"],
            "start": x["start_time"].isoformat(), "odds": round(x["odds"], 3), "p": round(x["p"], 4),
            "price": round(x["price"], 4)}


def bet_cycle(pred: pd.DataFrame) -> list[int]:
    if pred.empty:
        return []
    now = pd.Timestamp.now(tz="UTC")
    a = pred[(pred.approach == "A") & pred.price.notna()].copy()
    new_ids = []
    with engine().connect() as c:
        used = used_matches(c)
        n_par, n_val = today_count(c, "fav_parlay"), today_count(c, "value_single")
    # favourites: shortlist -> news -> parlays
    favs = a[(a.p >= FAV_P) & (a.p >= a.price) & (pd.to_datetime(a.start_time, utc=True) <= now +
                                                  pd.Timedelta(hours=NEWS_HOURS)) & ~a.match_id.isin(used)]
    ok = []
    for x in favs.sort_values("p", ascending=False).to_dict("records"):
        good, news = news_verdict(x)
        with session() as s:  # the news result as a version of the prediction (append-only)
            s.execute(insert(Prediction).values(match_id=int(x["match_id"]), market_id=int(x["market_id"]),
                                                outcome=x["side"], approach="A", model="tennis_v1_a+news",
                                                p=round(x["p"], 5), price=round(x["price"], 5),
                                                odds=round(x["odds"], 3), ev=round(x["ev"], 4), news=news,
                                                extra={"shortlist": True, "verdict": news["verdict"]}))
        if good:
            ok.append(x)
    pairs = []
    for i in range(len(ok)):
        for j in range(i + 1, len(ok)):
            if ok[i]["match_id"] == ok[j]["match_id"]:
                continue
            odds = ok[i]["odds"] * ok[j]["odds"]
            if odds >= PARLAY_MIN_ODDS:
                pairs.append((ok[i]["p"] * ok[j]["p"], odds, i, j))
    taken = set()
    for pj, odds, i, j in sorted(pairs, reverse=True):
        if n_par >= MAX_PARLAYS:
            break
        if i in taken or j in taken:
            continue
        new_ids.append(place("fav_parlay", "A", [leg_of(ok[i]), leg_of(ok[j])]))
        taken |= {i, j}
        n_par += 1
    # value singles (experimental: negative on the history)
    used |= {int(ok[k]["match_id"]) for k in taken}
    val = a[(a.p >= VALUE_MIN_P) & (a.ev >= VALUE_EDGE) & ~a.match_id.isin(used)]
    for x in val.sort_values("ev", ascending=False).drop_duplicates("match_id").to_dict("records"):
        if n_val >= MAX_SINGLES:
            break
        new_ids.append(place("value_single", "A", [leg_of(x)]))
        n_val += 1
    print(f"bets: {len(favs)} shortlisted, {len(ok)} passed the news, {len(new_ids)} new bets", flush=True)
    return new_ids


def settle() -> list[int]:
    done = []
    with engine().connect() as c:
        bets = c.execute(text("SELECT id, legs, odds, stake FROM paper_bets WHERE status = 'open'")).all()
        ids = list({int(leg["match_id"]) for _, legs, _, _ in bets for leg in legs})
        res = {m: (st, hs, aw) for m, st, hs, aw in c.execute(text(
            "SELECT id, status, home_score, away_score FROM matches WHERE id = ANY(:ids)"), {"ids": ids})}
    for bid, legs, odds, stake in bets:
        outcome, eff_odds = "won", 1.0
        for leg in legs:
            st, hs, aw = res.get(int(leg["match_id"]), (None, None, None))
            if st in ("walkover", "cancelled", "abandoned"):
                continue  # void leg
            if st != "finished" or hs is None or hs == aw:
                outcome = "open" if outcome == "won" else outcome
                continue
            won = (hs > aw) == (leg["outcome"] == "home")
            if not won:
                outcome = "lost"
            else:
                eff_odds *= leg["odds"]
        if outcome == "open":
            continue
        if outcome == "won" and eff_odds == 1.0:
            outcome = "void"
        pnl = Decimal(str(round(float(stake) * (eff_odds - 1), 2))) if outcome == "won" else \
            (-stake if outcome == "lost" else Decimal("0"))
        with session() as s:
            s.execute(text("UPDATE paper_bets SET status = :st, settled_at = now(), pnl = :pnl WHERE id = :id"),
                      {"st": outcome, "pnl": pnl, "id": bid})
        done.append(bid)
    return done


# ---------- telegram ----------

def send(text_: str) -> None:
    s = settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        print("telegram disabled:\n" + text_)
        return
    httpx.post(f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
               json={"chat_id": s.telegram_chat_id, "text": text_, "disable_web_page_preview": True}, timeout=15)


def fmt_bet(b) -> str:
    title = {"fav_parlay": "Экспресс фаворитов", "value_single": "Валуйный ординар (эксперимент)"}.get(b.kind, b.kind)
    lines = [f"🎾 {title} #{b.id} · бумажная ставка {b.stake:.0f} ₸"]
    for k, leg in enumerate(b.legs, 1):
        t = dt.datetime.fromisoformat(leg["start"]).astimezone(ALMATY)
        lines.append(f"{k}) {leg['match']} ({leg['league'][:40]}, {t:%d.%m %H:%M})\n"
                     f"   ставка: {leg['pick']}, кэф {leg['odds']:.2f} · модель {leg['p']:.0%}")
    lines.append(f"Общий кэф {float(b.odds):.2f} · вероятность по модели {float(b.p):.0%}")
    return "\n".join(lines)


def notify(new_ids: list[int], settled: list[int]) -> None:
    with engine().connect() as c:
        if new_ids:
            for b in c.execute(text("SELECT * FROM paper_bets WHERE id = ANY(:ids) ORDER BY id"), {"ids": new_ids}):
                send(fmt_bet(b))
        if settled:
            rows = c.execute(text("SELECT id, kind, status, odds, pnl FROM paper_bets WHERE id = ANY(:ids)"),
                             {"ids": settled}).all()
            mark = {"won": "✅", "lost": "❌", "void": "↩️"}
            send("Расчёт бумажных ставок:\n" + "\n".join(
                f"{mark.get(st, st)} #{i} {k} кэф {float(o):.2f}: {float(p):+.0f} ₸" for i, k, st, o, p in rows))
    if new_ids:
        with session() as s:
            s.execute(text("UPDATE paper_bets SET notified = true WHERE id = ANY(:ids)"), {"ids": new_ids})


def report() -> str:
    with engine().connect() as c:
        df = pd.read_sql(text("SELECT kind, approach, status, odds, stake, pnl FROM paper_bets"), c)
    if df.empty:
        return "Бумажных ставок пока нет."
    lines = ["Бумажные ставки:"]
    for (kind, appr), g in df.groupby(["kind", "approach"]):
        st = g[g.status.isin(["won", "lost"])]
        roi = float(st.pnl.sum() / st.stake.sum()) if len(st) else 0.0
        lines.append(f"{kind} ({appr}): всего {len(g)}, открыто {(g.status == 'open').sum()}, "
                     f"выиграно {(g.status == 'won').sum()}, проиграно {(g.status == 'lost').sum()}, "
                     f"P&L {float(g.pnl.fillna(0).sum()):+.0f} ₸, ROI {roi:+.1%}")
    return "\n".join(lines)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--send-report", action="store_true")
    args = ap.parse_args()
    if args.report or args.send_report:
        r = report()
        print(r)
        if args.send_report:
            send(r)
        return
    settled = [] if args.dry else settle()
    pred = predict_cycle(args.dry)
    new_ids = [] if args.dry else bet_cycle(pred)
    if args.dry and not pred.empty:
        a = pred[(pred.approach == "A") & pred.price.notna()]
        print(a.sort_values("p", ascending=False).head(12)[["home", "away", "side", "p", "price", "odds", "ev"]]
              .round(3).to_string())
    if not args.dry:
        notify(new_ids, settled)
    print(json.dumps({"settled": len(settled), "new_bets": len(new_ids)}))


if __name__ == "__main__":
    main()
