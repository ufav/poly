"""Tournament tier from the Polymarket title part after ' - ' (shared by analysis and the paper bot)."""

TIER1_KEYWORDS = ("iem", "blast", "esl pro league", "pgl", "major", "esports world cup", "starladder",
                  "perfect world", "fissure", "betboom dacha", "thunderpick world")
# Dota 2: the top events by Polymarket volume (2025-26); DreamLeague Division 2 is not tier1
DOTA2_TIER1_KEYWORDS = ("the international", "pgl wallachia", "blast slam", "dreamleague", "esports world cup",
                        "esl one", "riyadh masters", "games of the future", "fissure", "betboom dacha")
NOT_TIER1 = ("division 2",)


def tier_of(tournament: str | None, sport: str = "cs2") -> str:
    t = (tournament or "").lower()
    keys = DOTA2_TIER1_KEYWORDS if sport == "dota2" else TIER1_KEYWORDS
    top = any(k in t for k in keys) and not any(k in t for k in NOT_TIER1)
    if "qualifier" in t:
        return "tier1-qualifier" if top else "other-qualifier"
    return "tier1" if top else "tier2+"
