"""Dota 2: draft, heroes, combos, comfort picks, head-to-head, form — do they know more than the map price? (A-39)

Features per game (OpenDota league games, Radiant minus Dire), each computed only from earlier games:
  elo        team Elo (all league games since 06.2025)
  form       team win rate in its last 10 games
  h2h        smoothed log-odds of Radiant team vs Dire team in earlier meetings; h2h_n = log(1 + meetings)
  hero       sum of smoothed hero log-odds in the current patch
  synergy    sum over the 10 hero pairs of a side of pair log-odds (all patches)
  counter    sum over 25 Radiant-vs-Dire hero pairs of log-odds (Radiant view)
  team_hero  sum of team-on-hero log-odds; team_hero_n = sum log(1 + team games on hero)
  team_pair  sum log(1 + team games with this hero pair) — favourite combos
  player_hero / player_hero_n — the same for the player on his hero (account_id)
  rest_h, games_24h — hours since the previous game (cap 72), games in the last 24 h
Model: logistic regression (pure Python, Newton + small ridge), trained before --split, tested after.

Against Polymarket (maps linked in dota_map_links): price of the map market when the draft is known —
OpenDota start_time (lobby) + draft time + --delay s (horn ~120 s after the last pick). Check:
  1. y ~ logit(price) vs y ~ logit(price) + features on the test period (log loss);
  2. bets where the feature model disagrees with the price by more than a threshold, ROI after the taker fee
     (minute price ~ mid; spread not included), bootstrap over events.

  python analysis/dota_draft.py [--split 2026-04-15] [--delay 90]
"""

import argparse
import datetime as dt
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict, deque
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.executable import vwap  # noqa: E402
from pmbot import storage  # noqa: E402

FEATURES = ("elo", "form", "h2h", "h2h_n", "hero", "synergy", "counter", "team_hero", "team_hero_n", "team_pair",
            "player_hero", "player_hero_n", "rest_h", "games_24h")
GROUPS = {
    "team strength": ("elo", "form"),
    "head-to-head": ("h2h", "h2h_n"),
    "draft (heroes, combos, counters)": ("hero", "synergy", "counter"),
    "comfort (team/player on hero, favourite combos)": ("team_hero", "team_hero_n", "team_pair", "player_hero",
                                                        "player_hero_n"),
    "rest / fatigue": ("rest_h", "games_24h"),
}
BOOT = 300


def lo(w: float, n: float, k: float) -> float:
    """Log-odds of a win rate shrunk to 50% with k pseudo-games."""
    return math.log((w + k / 2) / (n - w + k / 2))


def logit(p: float) -> float:
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(min(x, 30), -30)))


class History:
    def __init__(self) -> None:
        self.elo = defaultdict(lambda: 1500.0)
        self.recent = defaultdict(lambda: deque(maxlen=10))
        self.times = defaultdict(list)
        self.h2h = defaultdict(lambda: [0, 0])          # (team, opp) -> [wins, n]
        self.hero_patch = defaultdict(lambda: [0, 0])   # (patch, hero)
        self.pair = defaultdict(lambda: [0, 0])         # (h1, h2) sorted, same side
        self.vs = defaultdict(lambda: [0, 0])           # (hero, enemy hero) from hero's view
        self.team_hero = defaultdict(lambda: [0, 0])
        self.team_pair = defaultdict(int)
        self.player_hero = defaultdict(lambda: [0, 0])

    def features(self, g: dict) -> dict[str, float]:
        r, d = g["teams"]
        side = []
        for team, heroes, players in ((r, g["heroes"][0], g["players"][0]), (d, g["heroes"][1], g["players"][1])):
            past = [x for x in self.times[team] if x < g["start"]]
            last = past[-1] if past else None
            side.append({
                "elo": self.elo[team] / 400,
                "form": (sum(self.recent[team]) / len(self.recent[team]) - 0.5) if self.recent[team] else 0.0,
                "hero": sum(lo(*self.hero_patch[(g["patch"], h)], 20) for h in heroes),
                "synergy": sum(lo(*self.pair[p], 20) for p in combinations(sorted(heroes), 2)),
                "team_hero": sum(lo(*self.team_hero[(team, h)], 10) for h in heroes),
                "team_hero_n": sum(math.log1p(self.team_hero[(team, h)][1]) for h in heroes),
                "team_pair": sum(math.log1p(self.team_pair[(team, *p)]) for p in combinations(sorted(heroes), 2)),
                "player_hero": sum(lo(*self.player_hero[(a, h)], 10) for a, h in players),
                "player_hero_n": sum(math.log1p(self.player_hero[(a, h)][1]) for a, h in players),
                "rest_h": min((g["start"] - last) / 3600, 72) if last else 72.0,
                "games_24h": sum(1 for x in past if x > g["start"] - 86400),
            })
        f = {k: side[0][k] - side[1][k] for k in side[0]}
        w, n = self.h2h[(r, d)]
        f["h2h"], f["h2h_n"] = lo(w, n, 6), math.log1p(n)
        f["counter"] = sum(lo(*self.vs[(a, b)], 20) for a in g["heroes"][0] for b in g["heroes"][1])
        return f

    def update(self, g: dict) -> None:
        r, d = g["teams"]
        y = g["radiant_win"]
        e = 1 / (1 + 10 ** ((self.elo[d] - self.elo[r]) / 400))
        self.elo[r] += 32 * (y - e)
        self.elo[d] -= 32 * (y - e)
        for team, opp, won, heroes, players in ((r, d, y, g["heroes"][0], g["players"][0]),
                                                (d, r, 1 - y, g["heroes"][1], g["players"][1])):
            self.recent[team].append(won)
            self.times[team].append(g["start"])
            self.h2h[(team, opp)][0] += won
            self.h2h[(team, opp)][1] += 1
            for h in heroes:
                for key, table in (((g["patch"], h), self.hero_patch), ((team, h), self.team_hero)):
                    table[key][0] += won
                    table[key][1] += 1
            for p in combinations(sorted(heroes), 2):
                self.pair[p][0] += won
                self.pair[p][1] += 1
                self.team_pair[(team, *p)] += 1
            for a, h in players:
                self.player_hero[(a, h)][0] += won
                self.player_hero[(a, h)][1] += 1
        for a in g["heroes"][0]:
            for b in g["heroes"][1]:
                self.vs[(a, b)][0] += y
                self.vs[(a, b)][1] += 1
                self.vs[(b, a)][0] += 1 - y
                self.vs[(b, a)][1] += 1


def load_games(conn: sqlite3.Connection) -> list[dict]:
    players = defaultdict(lambda: ([], []))
    for mid, slot, acc, hero in conn.execute("SELECT match_id, player_slot, account_id, hero_id FROM od_players"):
        players[mid][0 if slot < 128 else 1].append((acc, hero))
    games = []
    for mid, start, rw, rt, dt_, patch, timings in conn.execute(
            """SELECT match_id, start_time, radiant_win, radiant_team_id, dire_team_id, patch, draft_timings
               FROM od_matches WHERE radiant_win IS NOT NULL ORDER BY start_time"""):
        rad, dire = players[mid]
        if not rt or not dt_ or len(rad) != 5 or len(dire) != 5:
            continue
        draft = sum(x.get("total_time_taken") or 0 for x in json.loads(timings)) if timings else None
        games.append({"id": mid, "start": start, "radiant_win": int(rw), "teams": (rt, dt_), "patch": patch,
                      "heroes": ([h for _, h in rad], [h for _, h in dire]),
                      "players": ([p for p in rad if p[0]], [p for p in dire if p[0]]), "draft": draft})
    return games


def fit(xs: list[list[float]], ys: list[int], ridge: float = 1.0) -> list[float]:
    """Logistic regression by Newton steps; xs include the intercept column."""
    k = len(xs[0])
    w = [0.0] * k
    for _ in range(25):
        grad = [-ridge * w[j] if j else 0.0 for j in range(k)]
        hess = [[(ridge if i == j and i else 0.0) for j in range(k)] for i in range(k)]
        for x, y in zip(xs, ys):
            p = sigmoid(sum(a * b for a, b in zip(w, x)))
            for i in range(k):
                grad[i] += (y - p) * x[i]
                s = p * (1 - p) * x[i]
                for j in range(i, k):
                    hess[i][j] += s * x[j]
        for i in range(k):
            for j in range(i):
                hess[i][j] = hess[j][i]
        step = solve(hess, grad)
        w = [a + b for a, b in zip(w, step)]
        if max(abs(s) for s in step) < 1e-6:
            break
    return w


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        if abs(m[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for j in range(c, n + 1):
                    m[r][j] -= f * m[c][j]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def logloss(ps: list[float], ys: list[int]) -> float:
    return -sum(math.log(p if y else 1 - p) for p, y in zip(ps, ys)) / len(ys)


def standardizer(rows: list[dict], names: tuple) -> tuple[dict, dict]:
    mu = {f: sum(r[f] for r in rows) / len(rows) for f in names}
    sd = {f: (sum((r[f] - mu[f]) ** 2 for r in rows) / len(rows)) ** 0.5 or 1.0 for f in names}
    return mu, sd


def design(rows: list[dict], names: tuple, mu: dict, sd: dict, extra: list[float] | None = None) -> list[list[float]]:
    return [[1.0] + ([extra[i]] if extra else []) + [(r[f] - mu[f]) / sd[f] for f in names]
            for i, r in enumerate(rows)]


def predict(w: list[float], xs: list[list[float]]) -> list[float]:
    return [sigmoid(sum(a * b for a, b in zip(w, x))) for x in xs]


def price_at(conn: sqlite3.Connection, token: str, t: int) -> float | None:
    row = conn.execute("SELECT t, p FROM pm_prices WHERE token_id = ? AND t <= ? ORDER BY t DESC LIMIT 1",
                       (token, t)).fetchone()
    return row[1] if row and t - row[0] <= 300 else None


def roi_table(bets: list[tuple], rng: random.Random) -> str:
    """bets: (event, won, price) -> ROI per bet after the taker fee ± bootstrap over events."""
    if len(bets) < 30:
        return f"{len(bets):5d} bets"
    by_ev = defaultdict(list)
    for ev, won, p in bets:
        by_ev[ev].append(won / (p + 0.05 * p * (1 - p)) - 1)
    cells = list(by_ev.values())
    est = sum(sum(c) for c in cells) / len(bets)
    boots = []
    for _ in range(BOOT):
        pick = [cells[rng.randrange(len(cells))] for _ in cells]
        boots.append(sum(sum(c) for c in pick) / sum(len(c) for c in pick))
    m = sum(boots) / BOOT
    se = (sum((x - m) ** 2 for x in boots) / (BOOT - 1)) ** 0.5
    return f"{len(bets):5d} bets  ROI {est:+6.1%} ± {se:4.1%}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="2026-04-15")
    ap.add_argument("--warmup", default="2025-09-01", help="games before this only build history")
    ap.add_argument("--delay", type=int, default=90, help="s after the last pick for the market price")
    ap.add_argument("--window", type=int, default=180, help="s of taker trades after that moment for a real fill")
    ap.add_argument("--stake", type=float, default=100.0)
    args = ap.parse_args()
    conn = storage.connect(storage.db_path("dota2"))
    split = dt.datetime.fromisoformat(args.split).replace(tzinfo=dt.timezone.utc).timestamp()
    warm = dt.datetime.fromisoformat(args.warmup).replace(tzinfo=dt.timezone.utc).timestamp()

    games = load_games(conn)
    hist = History()
    rows = []
    for g in games:
        if g["start"] >= warm:
            rows.append({**hist.features(g), "y": g["radiant_win"], "id": g["id"], "start": g["start"],
                         "draft": g["draft"]})
        hist.update(g)
    train = [r for r in rows if r["start"] < split]
    test = [r for r in rows if r["start"] >= split]
    print(f"league games with teams and 5v5: {len(games)}; modelled {len(rows)} (train {len(train)}, test {len(test)})")
    print(f"Radiant win rate: train {sum(r['y'] for r in train) / len(train):.1%}, test {sum(r['y'] for r in test) / len(test):.1%}")

    mu, sd = standardizer(train, FEATURES)
    w = fit(design(train, FEATURES, mu, sd), [r["y"] for r in train])
    print("\nall league games — coefficients (per 1 sd):")
    print("   " + ", ".join(f"{f} {c:+.3f}" for f, c in zip(("intercept",) + FEATURES, w)))
    yt = [r["y"] for r in test]
    base = sum(r["y"] for r in train) / len(train)
    print(f"test log loss: Radiant rate {logloss([base] * len(test), yt):.4f}", end="")
    for label, names in (("elo only", ("elo",)), ("all features", FEATURES)):
        m2, s2 = standardizer(train, names)
        w2 = fit(design(train, names, m2, s2), [r["y"] for r in train])
        ps = predict(w2, design(test, names, m2, s2))
        acc = sum((p > 0.5) == bool(y) for p, y in zip(ps, yt)) / len(yt)
        print(f" | {label} {logloss(ps, yt):.4f} (accuracy {acc:.1%})", end="")
    print()
    for gname, names in GROUPS.items():
        rest = tuple(f for f in FEATURES if f not in names)
        m2, s2 = standardizer(train, rest)
        w2 = fit(design(train, rest, m2, s2), [r["y"] for r in train])
        print(f"   without {gname:48} test log loss {logloss(predict(w2, design(test, rest, m2, s2)), yt):.4f}")

    # Polymarket maps: price when the draft is known
    by_id = {r["id"]: r for r in rows}
    pm = []
    for od_id, ev, token, final_a, rw, cond in conn.execute(
            """SELECT l.od_match_id, e.id, m.token_a, m.final_price_a, o.radiant_win, m.condition_id
               FROM dota_map_links l JOIN od_matches o ON o.match_id = l.od_match_id
               JOIN pm_events e ON e.game_id = l.pm_game_id
               JOIN pm_markets m ON m.event_id = e.id AND m.market_type = 'child_moneyline'
                                 AND m.map_number = l.map_number
               WHERE m.final_price_a IN (0, 1) AND m.history_status = 'done'"""):
        r = by_id.get(od_id)
        if not r or r["draft"] is None:
            continue
        a_is_radiant = (final_a == 1) == bool(rw)
        t = r["start"] + r["draft"] + args.delay
        prices = [price_at(conn, token, t + extra) for extra in (0, 180, 300)]
        if prices[0] is None or not 0.03 < prices[0] < 0.97:
            continue
        pm.append({**r, "event": ev, "p": [None if p is None else (p if a_is_radiant else 1 - p) for p in prices],
                   "cond": cond, "token_a": token, "a_rad": a_is_radiant, "t": t})
    pm_train = [r for r in pm if r["start"] < split]
    pm_test = [r for r in pm if r["start"] >= split]
    print(f"\nPolymarket maps with price after the draft: {len(pm)} (train {len(pm_train)}, test {len(pm_test)})")

    yt = [r["y"] for r in pm_test]
    lp_train = [logit(r["p"][0]) for r in pm_train]
    lp_test = [logit(r["p"][0]) for r in pm_test]
    w0 = fit([[1.0, x] for x in lp_train], [r["y"] for r in pm_train], ridge=0.0)
    p0 = predict(w0, [[1.0, x] for x in lp_test])
    print(f"price only: slope {w0[1]:.3f}, test log loss {logloss(p0, yt):.4f} (raw price "
          f"{logloss([r['p'][0] for r in pm_test], yt):.4f})")
    m2, s2 = standardizer(pm_train, FEATURES)
    w1 = fit(design(pm_train, FEATURES, m2, s2, lp_train), [r["y"] for r in pm_train], ridge=5.0)
    p1 = predict(w1, design(pm_test, FEATURES, m2, s2, lp_test))
    print(f"price + features: test log loss {logloss(p1, yt):.4f}; coefficients: "
          + ", ".join(f"{f} {c:+.3f}" for f, c in zip(("intercept", "logit price") + FEATURES, w1)))

    # bets: feature model trained on all league games (no price) vs the market
    q = predict(w, design(pm_test, FEATURES, mu, sd))
    q_in = predict(w, design(pm_train, FEATURES, mu, sd))
    rng = random.Random(1)
    print("\nbets where the feature model (all league games before the split, no price) disagrees with the price:")
    for label, data, qs in (("train period (in-sample model)", pm_train, q_in), ("test period", pm_test, q)):
        print(f"  {label}")
        for thr in (0.03, 0.05, 0.08, 0.12):
            for k, when in enumerate(("at draft", "+3 min", "+5 min")):
                bets = []
                for r, qq in zip(data, qs):
                    p = r["p"][k]
                    if p is None or not 0.03 < p < 0.97:
                        continue
                    if qq - p > thr:
                        bets.append((r["event"], r["y"], p))
                    elif p - qq > thr:
                        bets.append((r["event"], 1 - r["y"], 1 - p))
                print(f"    edge > {thr:.2f} {when:9} {roi_table(bets, rng)}")

    # walk-forward: each month the model is fitted only on earlier league games
    print("\nwalk-forward by month (model on all earlier games, edge > 0.05 at draft; price-only log loss vs model):")
    months = sorted({dt.datetime.fromtimestamp(r["start"], dt.timezone.utc).strftime("%Y-%m") for r in pm})
    halves = defaultdict(list)
    for month in months[1:]:
        lo_ts = dt.datetime.fromisoformat(month + "-01").replace(tzinfo=dt.timezone.utc).timestamp()
        nxt = dt.datetime.fromtimestamp(lo_ts + 32 * 86400, dt.timezone.utc).strftime("%Y-%m")
        hi_ts = dt.datetime.fromisoformat(nxt + "-01").replace(tzinfo=dt.timezone.utc).timestamp()
        past = [r for r in rows if r["start"] < lo_ts]
        cur = [r for r in pm if lo_ts <= r["start"] < hi_ts]
        if len(past) < 2000 or not cur:
            continue
        mw, sw = standardizer(past, FEATURES)
        ww = fit(design(past, FEATURES, mw, sw), [r["y"] for r in past])
        qs = predict(ww, design(cur, FEATURES, mw, sw))
        bets, real = [], []
        for r, qq in zip(cur, qs):
            p = r["p"][0]
            if abs(qq - p) <= 0.05:
                continue
            radiant = qq > p
            won = r["y"] if radiant else 1 - r["y"]
            bets.append((r["event"], won, p if radiant else 1 - p))
            fill = executable(conn, r, radiant, args.window, args.stake)
            if fill is not None:
                real.append((r["event"], won, fill))
        half = "before " + args.split if lo_ts < split else "from " + args.split
        halves[half] += bets
        halves[half + ", real trades"] += real
        ys = [r["y"] for r in cur]
        league = [r for r in rows if lo_ts <= r["start"] < hi_ts]
        lq = predict(ww, design(league, FEATURES, mw, sw))
        print(f"  {month}: league games {len(league):5d} (model log loss {logloss(lq, [r['y'] for r in league]):.4f}) | "
              f"maps {len(cur):4d}, log loss price {logloss([r['p'][0] for r in cur], ys):.4f} "
              f"model {logloss(qs, ys):.4f} | {roi_table(bets, rng)} | real trades {roi_table(real, rng)}")
    for label, bets in halves.items():
        print(f"  {label}: {roi_table(bets, rng)}")


def executable(conn: sqlite3.Connection, r: dict, radiant: bool, window: int, stake: float) -> float | None:
    """Average price of the first `stake` $ of taker trades buying our side in [t, t + window] (None if less)."""
    on_a = radiant == r["a_rad"]
    tape = []
    for t, asset, side, price, size in conn.execute(
            "SELECT t, asset, side, price, size FROM pm_trades WHERE condition_id = ? AND t >= ? AND t <= ? ORDER BY t",
            (r["cond"], r["t"], r["t"] + window)):
        if ((asset == r["token_a"]) == (side == "BUY")) != on_a:
            continue
        p = price if side == "BUY" else 1 - price
        tape.append((t, p, price * size if side == "BUY" else (1 - price) * size))
    return vwap(tape, stake)


if __name__ == "__main__":
    main()
