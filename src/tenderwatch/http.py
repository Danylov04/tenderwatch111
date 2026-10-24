"""HTTP-клієнт: rate-limit на хост, ретраї з backoff, шанобливе ставлення до 429 / Retry-After."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from urllib.parse import urlparse

import httpx

log = logging.getLogger("tenderwatch.http")

RETRY_STATUS = {429, 500, 502, 503, 504}

# Заголовки, які зазвичай видають вендора/механізм WAF-блокування (Cloudflare, Akamai, Imperva,
# AWS WAF, rate-limit) або кукі-викличку — пишемо їх у лог при НЕ-ok відповіді, щоб за голим
# "HTTP 403" у логах Actions було видно хоч якийсь слід, а не гадати навмання (як було з ezam).
_DIAG_HEADER_KEYS = (
    "server", "via", "x-cache", "cf-ray", "cf-mitigated", "x-amzn-requestid", "x-amz-cf-id",
    "x-akamai-request-id", "x-waf-event-info", "x-sucuri-id", "retry-after", "content-type",
    "x-request-id", "set-cookie",
)


def _diag(resp: httpx.Response) -> str:
    """Короткий діагностичний знімок НЕ-ok відповіді: вибрані заголовки + початок тіла."""
    try:
        hdrs = {k: v for k, v in resp.headers.items() if k.lower() in _DIAG_HEADER_KEYS}
        body = resp.text[:300].replace("\n", " ")
    except Exception as e:  # noqa: BLE001 — діагностика сама не повинна валити запит
        return f"(діагностика не знята: {e})"
    return f"headers={hdrs!r} body[:300]={body!r}"


# 2026-10-24: саме завдяки _diag() з'ясувалось, що 403 на ezamowienia.gov.pl — НЕ бот-блок/WAF (як
# передбачалось спочатку), а сторінка-заглушка власника сайту: `Server: Microsoft-Azure-Application-
# Gateway/v2`, HTML з <title>e-Zamówienia - Przerwa techniczna</title> («технічна перерва»). Це тимчасове
# і нічим не лікується з нашого боку (жодні заголовки не допоможуть) — тому таку відповідь трактуємо
# окремо від «реальної» помилки: ретраїмо в межах прогону (може відновитись за хвилини), а якщо й після
# цього сайт у перерві — кидаємо MaintenanceError, а не звичайну HttpError, щоб викликаючий код (cli.py)
# міг не червонити CI через те, що не є нашим багом.
_MAINTENANCE_MARKERS = ("przerwa techniczna",)


def _is_maintenance_page(resp: httpx.Response) -> bool:
    try:
        if "html" not in resp.headers.get("content-type", "").lower():
            return False
        body = resp.text[:2000].lower()
    except Exception:  # noqa: BLE001
        return False
    return any(marker in body for marker in _MAINTENANCE_MARKERS)


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url


class MaintenanceError(HttpError):
    """Сайт сам каже, що тимчасово в технічній перерві (сторінка-заглушка власника) — не бот-блок і
    не наш баг. Відокремлена від HttpError, щоб виклик-код міг не вважати це критичним збоєм."""


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
        maintenance = False
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
            page_maintenance = _is_maintenance_page(resp)
            maintenance = maintenance or page_maintenance
            if resp.status_code in RETRY_STATUS or page_maintenance:
                delay = min(2 ** attempt, 60)
                ra = resp.headers.get("Retry-After")
                if ra and ra.isdigit():
                    delay = min(int(ra), 120)
                log.warning(
                    "HTTP %s on %s (try %d), sleeping %ss — %s%s", resp.status_code, url, attempt, delay, _diag(resp),
                    " [технічна перерва на сайті]" if page_maintenance else "",
                )
                last_exc = HttpError(f"HTTP {resp.status_code}", resp.status_code, url)
                self._sleep(delay)
                continue
            log.warning("HTTP %s for %s — %s", resp.status_code, url, _diag(resp))
            raise HttpError(f"HTTP {resp.status_code} for {url}", resp.status_code, url)
        if maintenance:
            raise MaintenanceError(f"сайт у технічній перерві: {url}", getattr(last_exc, "status", None), url)
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
