"""Chat completions through any OpenAI-compatible API (Groq by default, OpenRouter by LLM_BASE_URL), with a JSON
answer; the provider and model are configuration (docs/md1.md 7.2), the code does not depend on them."""

from __future__ import annotations

import json
import time

import httpx

from core.config import settings


def _key(base_url: str) -> str:
    s = settings()
    if "openrouter" in base_url:
        return s.openrouter_api_key
    return s.groq_api_key


def chat_json(system: str, user: str, model: str | None = None, temperature: float = 0.1,
              max_tokens: int = 2500, retries: int = 3) -> dict:
    """The model's answer parsed as a JSON object (the prompt must ask for one)."""
    s = settings()
    url = f"{s.llm_base_url.rstrip('/')}/chat/completions"
    body = {"model": model or s.llm_model, "temperature": temperature, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    for attempt in range(retries):
        r = httpx.post(url, json=body, headers={"Authorization": f"Bearer {_key(s.llm_base_url)}"}, timeout=120)
        if r.status_code == 429 or r.status_code >= 500:  # rate limit of the free tier / provider hiccup
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]["content"]
        try:
            return json.loads(msg)
        except json.JSONDecodeError:
            start, end = msg.find("{"), msg.rfind("}")
            if start >= 0 and end > start:
                return json.loads(msg[start:end + 1])
            raise
    r.raise_for_status()
    return {}
