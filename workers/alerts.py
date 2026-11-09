"""Telegram alerts of failing jobs (docs/md1.md 4.1 Monitoring & Operations): a message when a job / pipeline step
fails THRESHOLD runs in a row, and one when it works again - single failures (ESPN unreachable for an hour) stay
quiet. The counters live in data/alerts_state.json so they survive the worker's restarts."""

from __future__ import annotations

import json
import logging

import httpx

from core.config import settings

log = logging.getLogger("alerts")
STATE = settings().data_dir / "alerts_state.json"


def send(text: str) -> None:
    s = settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        log.info("telegram disabled: %s", text)
        return
    try:
        httpx.post(f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
                   json={"chat_id": s.telegram_chat_id, "text": text}, timeout=15)
    except httpx.HTTPError as e:  # an alert must never break the job runner
        log.warning("telegram alert failed: %r", e)


def track(name: str, ok: bool, threshold: int = 3) -> None:
    try:
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
    except (OSError, ValueError):
        state = {}
    fails = int(state.get(name, 0))
    if ok:
        if fails >= threshold:
            send(f"✅ {name}: снова работает (после {fails} сбоев подряд)")
        state[name] = 0
    else:
        fails += 1
        state[name] = fails
        if fails == threshold:
            send(f"⚠️ {name}: {fails} сбоя подряд — проверь логи: docker compose logs --tail 200 worker")
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state))
    except OSError as e:
        log.warning("alerts state not saved: %r", e)
