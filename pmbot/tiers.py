"""Tournament tier from the Polymarket title part after ' - ' (shared by analysis and the paper bot)."""

TIER1_KEYWORDS = ("iem", "blast", "esl pro league", "pgl", "major", "esports world cup", "starladder",
                  "perfect world", "fissure", "betboom dacha", "thunderpick world")


def tier_of(tournament: str | None) -> str:
    t = (tournament or "").lower()
    if "qualifier" in t:
        return "tier1-qualifier" if any(k in t for k in TIER1_KEYWORDS) else "other-qualifier"
    return "tier1" if any(k in t for k in TIER1_KEYWORDS) else "tier2+"
