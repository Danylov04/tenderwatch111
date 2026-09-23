"""HTTP-клієнт: rate-limit на хост, ретраї з backoff, шанобливе ставлення до 429 / Retry-After."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from urllib.parse import urlparse

import httpx

log = logging.getLogger("tenderwatch.http")

RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url


class Http:
    def __init__(
        self,
        user_agent: str = "tenderwatch/0.1",
        interval: float = 0.35,
        host_intervals: dict[str, float] | None = None,
        max_tries: int = 5,
        timeout: float = 40.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "pl,en;q=0.7"},
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )
        self.interval = interval
        self.host_intervals = host_intervals or {}
        self.max_tries = max_tries
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}
        self.requests = 0

    def close(self) -> None:
        self._client.close()

    def _throttle(self, host: str) -> None:
        gap = self.host_intervals.get(host, self.interval)
        last = self._last.get(host)
        if last is not None:
            wait = gap - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last[host] = self._clock()

    def request(self, method: str, url: str, *, ok: tuple[int, ...] = (200,), **kw) -> httpx.Response:
        host = urlparse(url).netloc
        last_exc: Exception | None = None
        for attempt in range(1, self.max_tries + 1):
            self._throttle(host)
            self.requests += 1
            try:
                resp = self._client.request(method, url, **kw)
            except httpx.TransportError as e:  # мережа/таймаут
                last_exc = e
                log.warning("transport error %s on %s (try %d)", e, url, attempt)
                self._sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code in ok:
                return resp
            if resp.status_code in RETRY_STATUS:
                delay = min(2 ** attempt, 60)
                ra = resp.headers.get("Retry-After")
                if ra and ra.isdigit():
                    delay = min(int(ra), 120)
                log.warning("HTTP %s on %s (try %d), sleeping %ss", resp.status_code, url, attempt, delay)
                last_exc = HttpError(f"HTTP {resp.status_code}", resp.status_code, url)
                self._sleep(delay)
                continue
            raise HttpError(f"HTTP {resp.status_code} for {url}", resp.status_code, url)
        if isinstance(last_exc, HttpError):
            raise last_exc
        raise HttpError(f"giving up on {url}: {last_exc}", None, url)

    def get(self, url: str, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

    def get_json(self, url: str, **kw):
        resp = self.get(url, **kw)
        try:
            return resp.json()
        except ValueError as e:
            raise HttpError(f"invalid JSON from {url}: {e}", resp.status_code, url) from e

    def post_json(self, url: str, payload: dict, **kw):
        resp = self.request("POST", url, json=payload, **kw)
        try:
            return resp.json()
        except ValueError as e:
            raise HttpError(f"invalid JSON from {url}: {e}", resp.status_code, url) from e
