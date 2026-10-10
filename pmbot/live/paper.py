"""Paper trading: rules evaluated on live score, fills simulated on the real order book, settlement from Gamma.

Rules are hypotheses to test, not proven strategies (see docs/research.md). All fire once per map at halftime
(12-13 rounds played; MR12, one missed WebSocket update tolerated).
"""

import json
from dataclasses import dataclass

from pmbot.join import norm_team

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


RULES = {
    "fav_comeback_half": fav_comeback(0.70, range(2, 5)),
    "fav_comeback_wide": fav_comeback(0.60, range(2, 7)),
    "leader_half": leader(4),
    "leader_half6": leader(6),
}


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
