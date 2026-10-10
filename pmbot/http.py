import time

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)


class HttpClient:
    """httpx wrapper with a browser User-Agent, polite pacing and retries."""

    def __init__(self, min_interval: float = 0.15, retries: int = 5, timeout: float = 30):
        self._client = httpx.Client(headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=timeout)
        self._min_interval = min_interval
        self._retries = retries
        self._last = 0.0

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        for attempt in range(self._retries):
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self._client.get(url, params=params)
            except httpx.TransportError:
                time.sleep(2**attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2**attempt)
                continue
            return r
        r.raise_for_status()
        return r

    def get_json(self, url: str, params: dict | None = None):
        r = self.get(url, params)
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._client.close()
