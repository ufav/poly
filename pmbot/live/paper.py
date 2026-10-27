"""Paper trading: rules evaluated on live score, fills simulated on the real order book, settlement from Gamma.

Rules are hypotheses to test, not proven strategies (see docs/research.md). Each fires at most once per map:
halftime leaders at 12-13 rounds (MR12); late / maker / comebacks are opt-in or removed. Price rules (tier1):
fav_band taker and maker. Dota 2 (tier1 BO3, R-20): map 2 after lost map 1 by match prematch.
Tennis (R-23b): after set 1, buy the set-1 winner (any / prematch dog / prematch fav).
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


def map1_loser(lo: float, hi: float):
    """BO3 map 2: the side that lost map 1, if its match price at scheduled start is in [lo, hi)."""
    def rule(s: MapState) -> str | None:
        if s.map_number != 2 or sorted(s.maps.values()) != [0, 1]:
            return None
        team = min(s.maps, key=s.maps.get)
        return team if lo <= s.prematch[team] < hi else None
    return rule


def set1_winner(lo: float, hi: float):
    """After set 1: buy the set-1 winner if its match price at scheduled start is in [lo, hi)."""
    def rule(s: MapState) -> str | None:
        if s.map_number != 1 or sorted(s.maps.values()) != [0, 1]:
            return None
        team = max(s.maps, key=s.maps.get)
        return team if lo <= s.prematch[team] < hi else None
    return rule


RULES = {
    "leader_half": leader(4),
    "leader_half6": leader(6),
    "leader_late": leader_late(5, 11),  # off by default (R-14/R-26 minus); enable via PAPER_RULES
}

# ask below MIN means the market disagrees with the feed score (broken feed of minor events)
MIN_ASK = {"leader_half": 0.55, "leader_half6": 0.60, "leader_late": 0.60}

# price rules, tier1 only (R-14 / R-15: in tier1 live the favourite priced 0.70-0.92 is underpriced by 2-3 pp)
BAND_RULES = {"fav_band_t1": (0.70, 0.92)}   # taker at the half: buy the side whose ask is in the band
MAKER_RULES = {"maker_t1": (0.70, 0.92)}     # resting bid on the favourite, filled only when a taker trades through

# Dota 2 tier1 BO3, map 2 after a lost map 1 (R-20): underdog at odds 2.0-2.5 / 2.5-3.33, and any map 1 loser
DOTA_RULES = {
    "dota_dog_m2": map1_loser(0.40, 0.50),
    "dota_dog3_m2": map1_loser(0.30, 0.40),
    "dota_loser_m2": map1_loser(0.0, 1.01),
}

# Tennis moneyline after set 1 (R-23b): buy set-1 winner (any / prematch dog / prematch fav)
TENNIS_RULES = {
    "tennis_s1_winner": set1_winner(0.0, 1.01),
    "tennis_dog_s1": set1_winner(0.0, 0.50),
    "tennis_fav_s1": set1_winner(0.50, 1.01),
}
SPORT_RULES = {
    "cs2": set(RULES) | set(BAND_RULES) | set(MAKER_RULES),
    "dota2": set(DOTA_RULES),
    "tennis": set(TENNIS_RULES),
}

# when PAPER_RULES empty
DEFAULT_RULES = {
    "cs2": ("leader_half", "leader_half6", "fav_band_t1"),
    "dota2": tuple(DOTA_RULES),
    "tennis": tuple(TENNIS_RULES),
}


def is_tier1(game: dict, sport: str = "cs2") -> bool:
    return tier_of(game.get("tournament"), sport) == "tier1"


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


def parse_maps(score: str | None) -> tuple[int, int] | None:
    """'000-000|1-0|Bo3' -> (1, 0): maps won in home-away order, also when kills / rounds are unknown."""
    parts = (score or "").split("|")
    if len(parts) < 2 or "-" not in parts[1]:
        return None
    try:
        a, b = (int(x) for x in parts[1].split("-"))
    except ValueError:
        return None
    return a, b


def _same_team(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 3 and (a in b or b in a))


def olimpbet_match(events: list[dict], home: str | None, away: str | None) -> dict | None:
    """The only Olimpbet live event (olimpbet.parse_live) of the WS teams -> {rounds, maps} in WS home-away order
    and map_number (maps listed in its score, the current one included); None if none or ambiguous."""
    h, a = norm_team(home), norm_team(away)
    if not h or not a:
        return None
    found = []
    for ev in events:
        eh, ea = norm_team(ev["home"]), norm_team(ev["away"])
        if _same_team(h, eh) and _same_team(a, ea):
            found.append((ev, False))
        elif _same_team(h, ea) and _same_team(a, eh):
            found.append((ev, True))
    if len(found) != 1:
        return None
    ev, swap = found[0]
    rounds, maps = ev["periods"][-1], ev["maps"]
    if swap:
        rounds, maps = rounds[::-1], maps[::-1]
    return {"rounds": rounds, "maps": maps, "map_number": len(ev["periods"])}


def parse_tennis_sets(score: str | None) -> list[tuple[int, int]] | None:
    """'6-3, 4-3' / '4-6, 0-0' -> [(6, 3), (4, 3)] games per set in home-away order."""
    if not score or "|" in score:
        return None
    sets = []
    for part in score.split(","):
        part = part.strip()
        if "-" not in part:
            return None
        try:
            a, b = (int(x) for x in part.split("-", 1))
        except ValueError:
            return None
        sets.append((a, b))
    return sets or None


def tennis_set_complete(home: int, away: int) -> bool:
    """Standard set: lead by 2 at 6+, or 7 (tiebreak / 7-5)."""
    hi, lo = max(home, away), min(home, away)
    return (hi >= 6 and hi - lo >= 2) or hi == 7


def tennis_period_set(period: str | None) -> int | None:
    """'S1' / 'S2' / 'S3' -> 1/2/3."""
    m = str(period or "").strip().upper()
    if len(m) >= 2 and m[0] == "S" and m[1:].isdigit():
        return int(m[1:])
    return None


def moneyline_market(game: dict) -> dict | None:
    return next((m for m in game["markets"] if m["market_type"] == "moneyline"), None)


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
