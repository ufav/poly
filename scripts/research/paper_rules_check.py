"""Offline sanity checks of pmbot/live/paper.py: score parsing, rules, fills, market choice."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot.live import paper  # noqa: E402

assert paper.parse_score("7-1|0-0|Bo3") == ((7, 1), (0, 0))
assert paper.parse_score("000-000|1-1|Bo3") is None
assert paper.parse_score(None) is None

# slight fav leading at half — prematch in band odds ~2.00–1.54
pre = {"Vitality": 0.40, "Aurora": 0.60}
s = paper.MapState(1, {"Vitality": 3, "Aurora": 9}, {"Vitality": 0, "Aurora": 0}, pre)
assert paper.RULES["leader_half"](s) == "Aurora"
assert paper.RULES["leader_half6"](s) == "Aurora"
# underdog leading — prematch 0.40 outside band → skip
pre_dog = {"Vitality": 0.60, "Aurora": 0.40}
s_dog = paper.MapState(1, {"Vitality": 3, "Aurora": 9}, {"Vitality": 0, "Aurora": 0}, pre_dog)
assert paper.RULES["leader_half"](s_dog) is None

# heavy favourite leading — prematch outside band → no signal (R-26)
pre_fav = {"Vitality": 0.85, "Aurora": 0.15}
s = paper.MapState(1, {"Vitality": 9, "Aurora": 3}, {"Vitality": 0, "Aurora": 0}, pre_fav)
assert paper.RULES["leader_half"](s) is None
assert paper.RULES["leader_half6"](s) is None

# not at half / lead too small
s = paper.MapState(1, {"Vitality": 5, "Aurora": 6}, {"Vitality": 0, "Aurora": 0}, pre)
assert paper.RULES["leader_half"](s) is None
assert paper.RULES["leader_half6"](s) is None

assert "fav_comeback_half" not in paper.SPORT_RULES["cs2"]
assert set(paper.DEFAULT_RULES["cs2"]) == {"leader_half", "leader_half6", "fav_band_t1"}
assert paper.DEFAULT_RULES["tennis"] == ("tennis_fav_s1",)
assert paper.MAX_ASK["leader_half6"] == 0.80
assert paper.MIN_ASK["tennis_fav_s1"] == 0.77
assert paper.MAX_ASK["tennis_fav_s1"] == 0.91

fill = paper.simulate_fill([(0.40, 100), (0.42, 1000)], 100)
assert abs(fill["spent"] - 100) < 1e-6 and 0.40 < fill["avg_price"] < 0.42, fill
thin = paper.simulate_fill([(0.40, 10)], 100)
assert abs(thin["filled"] - 0.04) < 1e-9, thin

game = {"best_of": 3, "markets": [
    {"market_type": "moneyline", "map_number": None, "outcome_a": "A", "outcome_b": "B", "token_a": "m1", "token_b": "m2"},
    {"market_type": "child_moneyline", "map_number": 1, "outcome_a": "A", "outcome_b": "B", "token_a": "a1", "token_b": "b1"},
]}
assert paper.map_market(game, 1, (0, 0))["token_a"] == "a1"
assert paper.map_market(game, 2, (1, 0)) is None
assert paper.map_market(game, 3, (1, 1))["token_a"] == "m1"
assert paper.token_for(game["markets"][1], "B") == "b1"
print("ok")
