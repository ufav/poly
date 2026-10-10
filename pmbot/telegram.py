"""Telegram notifications; silently disabled when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set."""

import logging
import os

import httpx

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID") or ""
        self.enabled = bool(self.token and self.chat_id)

    def send(self, text: str) -> None:
        if not self.enabled:
            log.info("telegram (disabled): %s", text.replace("\n", " | "))
            return
        try:
            httpx.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                       json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}, timeout=15)
        except httpx.HTTPError as e:
            log.warning("telegram send failed: %r", e)
