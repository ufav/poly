"""News search for the shortlisted matches (docs/md1.md 7; the LLM sees only what this finds): Tavily search API,
recent articles about a match / team / player, with the page text, url, source and publication date.

  python -m ai_engine.news_search "Arsenal injury news"
"""

from __future__ import annotations

import sys

import httpx

from core.config import settings

API = "https://api.tavily.com/search"


def search(query: str, days: int = 3, max_results: int = 5, topic: str = "news") -> list[dict]:
    """Recent results: [{title, url, content, score, published_date}]."""
    key = settings().tavily_api_key
    if not key:
        raise RuntimeError("TAVILY_API_KEY is not set in .env")
    r = httpx.post(API, json={"query": query, "topic": topic, "days": days, "max_results": max_results,
                              "search_depth": "basic", "include_answer": False},
                   headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    return [{k: x.get(k) for k in ("title", "url", "content", "score", "published_date")}
            for x in r.json().get("results") or []]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for x in search(" ".join(sys.argv[1:]) or "Premier League team news injuries"):
        print(f"{x['published_date']}  {x['title'][:80]}\n    {x['url']}\n    {(x['content'] or '')[:200]}\n")
