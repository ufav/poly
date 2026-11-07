"""News check of a shortlisted match (docs/md1.md 7.3 News Classifier + Fact Checker, 7.4 result structure):
Tavily finds the recent articles about the match and both sides, the LLM extracts only the facts written in them -
injuries, suspensions, lineup / rotation, coach, motivation, conflicts, fatigue / travel, weather - each with the
subject, team, importance, reliability, whether it is confirmed, the model features it touches and the source.
The LLM does not change probabilities (7.5): the facts go to the features, the model recalculates.

  python -m ai_engine.news_check "Arsenal" "Chelsea" --sport football --league "Premier League" --date 2026-11-09
"""

from __future__ import annotations

import argparse
import json
import sys

from ai_engine import llm
from ai_engine.news_search import search

SYSTEM = """You are a sports news analyst for a betting model. You get articles (title, date, url, text) about an
upcoming match. Extract ONLY facts that are written in these articles and matter for this match: injuries,
suspensions, expected lineup / rotation / rested players, coach changes, motivation (title race, relegation,
nothing to play for, cup priority), conflicts, fatigue / travel, weather, withdrawals (tennis).
Rules: never invent facts; list only actual problems or changes (an absence, an injury, a doubt, a rest) - never
"no injury", "not on the injury list", "fully fit" as a fact (put such reassurance in "summary"); never estimate win
probabilities or odds; if articles contradict each other, say so in
"uncertainties"; a rumour or social-media claim is not confirmed; ignore articles about other matches or old seasons.
Answer one JSON object:
{"facts": [{"category": "injury|suspension|lineup|rotation|coach|motivation|conflict|fatigue|weather|withdrawal|other",
            "subject_type": "player|team|coach", "subject": "...", "team": "...", "importance": 0..1,
            "reliability": 0..1, "confirmed": true|false,
            "affected_features": ["team_strength", "player_availability", "attack", "defence", "goalkeeper",
                                  "motivation", "fatigue", "lineup"],
            "description": "...", "source_url": "...", "published": "..."}],
 "uncertainties": ["..."],
 "red_flags": ["short reasons to doubt a bet on either side, if any"],
 "summary": "2-3 sentences"}"""


def articles(home: str, away: str, sport: str, days: int = 3) -> list[dict]:
    queries = [f"{home} vs {away} {sport} team news", f"{home} injury news", f"{away} injury news"]
    if sport == "tennis":
        queries = [f"{home} vs {away} tennis", f"{home} tennis injury", f"{away} tennis injury"]
    seen, out = set(), []
    for q in queries:
        for x in search(q, days=days, max_results=5):
            if x["url"] in seen:
                continue
            seen.add(x["url"])
            out.append(x)
    return out


def check(home: str, away: str, sport: str, league: str = "", date: str = "", days: int = 3) -> dict:
    arts = articles(home, away, sport, days)
    if not arts:
        return {"facts": [], "uncertainties": ["no recent articles found"], "red_flags": [], "summary": "",
                "articles": 0}
    text = "\n\n".join(f"[{i + 1}] {a['title']} ({a.get('published_date') or 'no date'})\n{a['url']}\n"
                       f"{(a.get('content') or '')[:1200]}" for i, a in enumerate(arts))
    user = (f"Match: {home} vs {away} ({sport}{', ' + league if league else ''}{', ' + date if date else ''}).\n"
            f"Articles:\n\n{text}")
    out = llm.chat_json(SYSTEM, user)
    out["articles"] = len(arts)
    out["sources"] = [a["url"] for a in arts]
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("home")
    ap.add_argument("away")
    ap.add_argument("--sport", default="football")
    ap.add_argument("--league", default="")
    ap.add_argument("--date", default="")
    a = ap.parse_args()
    print(json.dumps(check(a.home, a.away, a.sport, a.league, a.date), ensure_ascii=False, indent=2))
