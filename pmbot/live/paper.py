"""Paper trading: rules evaluated on live score, fills simulated on the real order book, settlement from Gamma.

Rules are hypotheses to test, not proven strategies (see docs/research.md). Each fires at most once per map:
halftime rules at 12-13 rounds played (MR12, one missed WebSocket update tolerated), late rules in the 2nd half.
Price rules (tier1 only): a taker buy of the side priced in a band at the half, and a paper maker whose bid on the
favourite is filled from the taker tape (one position per map, up to the stake).
"""

import json
import math
from dataclasses import dataclass, field
from itertools import groupby

from pmbot.join import norm_team
from pmbot.tiers import tier_of

FEE_RATE = 0.05  # taker fee per share = FEE_RATE * p * (1 - p)
HALF_ROUNDS = (12, 13)


@dataclass
class MapState:
    map_number: int
    rounds: dict[str, int]      # team -> rounds won on the current map
    maps: dict[str, int]        # team -> maps won before this one
    prematch: dict[str, float]  # team -> match price at scheduled start

    @property
    def total(self) -> int:
        return sum(self.rounds.values())

    def favorite(self, min_price: float) -> str | None:
        return next((t for t, p in self.prematch.items() if p >= min_price), None)

    def other(self, team: str) -> str:
        return next(t for t in self.rounds if t != team)


def _at_half(s: MapState) -> bool:
    return HALF_ROUNDS[0] <= s.total <= HALF_ROUNDS[1]


def fav_comeback(min_fav: float, deficits: range):
    def rule(s: MapState) -> str | None:
        fav = s.favorite(min_fav)
        if not fav or not _at_half(s):
            return None
        return fav if s.rounds[s.other(fav)] - s.rounds[fav] in deficits else None
    return rule


def leader(min_lead: int):
    def rule(s: MapState) -> str | None:
        if not _at_half(s):
            return None
        top = max(s.rounds, key=s.rounds.get)
        return top if s.rounds[top] - s.rounds[s.other(top)] >= min_lead else None
    return rule


def leader_late(min_lead: int, min_rounds: int):
    """2nd half of regulation: leader with >= min_rounds won and lead >= min_lead (R-12)."""
    def rule(s: MapState) -> str | None:
        if not 13 <= s.total <= 24:
            return None
        top = max(s.rounds, key=s.rounds.get)
        lead = s.rounds[top] - s.rounds[s.other(top)]
        return top if min_rounds <= s.rounds[top] < 13 and lead >= min_lead else None
    return rule


RULES = {
    "fav_comeback_half": fav_comeback(0.70, range(2, 5)),
    "fav_comeback_wide": fav_comeback(0.60, range(2, 7)),
    "leader_half": leader(4),
    "leader_half6": leader(6),
    "leader_late": leader_late(5, 11),
}

# the leader's ask below this means the market disagrees with the feed score (broken feed of minor events)
MIN_ASK = {"leader_half": 0.55, "leader_half6": 0.60, "leader_late": 0.60}

# price rules, tier1 only (R-14 / R-15: in tier1 live the favourite priced 0.70-0.92 is underpriced by 2-3 pp)
BAND_RULES = {"fav_band_t1": (0.70, 0.92)}   # taker at the half: buy the side whose ask is in the band
MAKER_RULES = {"maker_t1": (0.70, 0.92)}     # resting bid on the favourite, filled only when a taker trades through
ALL_RULES = set(RULES) | set(BAND_RULES) | set(MAKER_RULES)


def is_tier1(game: dict) -> bool:
    return tier_of(game.get("tournament")) == "tier1"


@dataclass
class MakerQuote:
    """Paper maker state of one market: reference = last traded price of token A (from the taker tape)."""
    ref: float | None = None
    ref_t: int = 0
    primed: bool = False
    seen: set = field(default_factory=set)


def maker_fills(q: MakerQuote, trades: list[dict], token_a: str, lo: float, hi: float,
                max_age: int = 60) -> list[tuple[bool, float, float]]:
    """New Data API taker trades -> fills of our bid (fav_is_a, bid, taker notional at our bid).

    Our bid on the side priced in [lo, hi] = reference floored to the cent, re-quoted after every second of tape.
    Filled only by a taker selling that side below our bid (the level was traded through - queue-proof, as the
    'strict' rule of analysis/maker_sim.py). The first call only primes the reference (no fills on old tape)."""
    new = sorted((x for x in trades if _trade_key(x) not in q.seen), key=lambda x: int(x["timestamp"]))
    fills = []
    for t, group in groupby(new, key=lambda x: int(x["timestamp"])):
        group = list(group)
        q.seen.update(_trade_key(x) for x in group)
        if q.primed and q.ref is not None and q.ref_t <= t <= q.ref_t + max_age:
            fav_a = True if lo <= q.ref <= hi else False if lo <= 1 - q.ref <= hi else None
            if fav_a is not None:
                bid = math.floor((q.ref if fav_a else 1 - q.ref) * 100 + 1e-9) / 100
                for x in group:
                    on_a = x["asset"] == token_a
                    p_a = float(x["price"]) if on_a else 1 - float(x["price"])
                    if ((x["side"] == "BUY") == on_a) == fav_a:  # taker buys the favourite: not our side
                        continue
                    if (p_a if fav_a else 1 - p_a) < bid - 1e-9:
                        fills.append((fav_a, bid, float(x["size"]) * bid))
        if t >= q.ref_t:
            last = group[-1]
            q.ref = float(last["price"]) if last["asset"] == token_a else 1 - float(last["price"])
            q.ref_t = t
    q.primed = True
    return fills


def _trade_key(x: dict) -> tuple:
    return x.get("transactionHash"), x["asset"], x["side"], x["price"], x["size"]


def consistent(map_number: int, rounds: tuple[int, int], maps: tuple[int, int]) -> bool:
    """Feed sanity: the map count matches the period (no score left over from the previous map) and the rounds are
    a reachable MR12 score (overtime only from 12-12)."""
    if map_number != sum(maps) + 1:
        return False
    if sum(rounds) <= 24:
        return max(rounds) <= 13
    return min(rounds) >= 12


def parse_score(score: str | None) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """'7-1|0-0|Bo3' -> ((7, 1), (0, 0)) in home-away order; None when rounds are unknown ('000-000')."""
    if not score or "|" not in score:
        return None
    parts = score.split("|")
    if parts[0] == "000-000" or "-" not in parts[0] or "-" not in parts[1]:
        return None
    try:
        r = tuple(int(x) for x in parts[0].split("-"))
        m = tuple(int(x) for x in parts[1].split("-"))
    except ValueError:
        return None
    return (r[0], r[1]), (m[0], m[1])


def map_market(game: dict, map_number: int, maps_won: tuple[int, int]) -> dict | None:
    """Market deciding the current map: 'Map N Winner', or the match market for a deciding map / BO1."""
    child = next((m for m in game["markets"] if m["market_type"] == "child_moneyline" and m["map_number"] == map_number),
                 None)
    if child:
        return child
    best_of = game.get("best_of") or 1
    need = best_of // 2
    if best_of == 1 or (maps_won[0] == need and maps_won[1] == need):
        return next((m for m in game["markets"] if m["market_type"] == "moneyline"), None)
    return None


def token_for(market: dict, team: str) -> str | None:
    if norm_team(market["outcome_a"]) == norm_team(team):
        return market["token_a"]
    if norm_team(market["outcome_b"]) == norm_team(team):
        return market["token_b"]
    return None


def simulate_fill(asks: list[tuple[float, float]], stake: float) -> dict:
    """Walk the asks (price ascending) spending up to `stake` USD."""
    spent = shares = 0.0
    for price, size in asks:
        take = min(size, (stake - spent) / price)
        spent += take * price
        shares += take
        if spent >= stake - 1e-9:
            break
    avg = spent / shares if shares else None
    fee = shares * FEE_RATE * avg * (1 - avg) if avg else 0.0
    return {"spent": spent, "shares": shares, "avg_price": avg, "fee": fee, "filled": spent / stake if stake else 0}


def settle(market_json: dict, token: str) -> float | None:
    """Payout per share (1/0) once the market is closed, else None."""
    if not market_json.get("closed"):
        return None
    tokens = json.loads(market_json.get("clobTokenIds") or "[]")
    prices = json.loads(market_json.get("outcomePrices") or "[]")
    if token not in tokens or len(prices) != len(tokens):
        return None
    p = float(prices[tokens.index(token)])
    return p if p in (0.0, 1.0) else None
