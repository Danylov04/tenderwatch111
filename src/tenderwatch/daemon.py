"""Планувальник без зовнішніх залежностей: стан розкладу зберігається в БД, тож перезапуск контейнера нічого не губить."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from . import notify, pipeline
from .config import Rules, Settings
from .db import kv_get, kv_set
from .http import Http
from .sources import build
from .util import KYIV, WARSAW, iso, parse_dt, utcnow

log = logging.getLogger("tenderwatch.daemon")

SWEEP_CAPABLE = {"ezam"}

HOST_INTERVALS = {
    "platformazakupowa.pl": 1.2,
    "api.ted.europa.eu": 0.6,
    "bazakonkurencyjnosci.gov.pl": 0.6,
}


def make_http(settings: Settings, **kw) -> Http:
    return Http(user_agent=settings.user_agent, interval=settings.request_interval, host_intervals=HOST_INTERVALS, **kw)


def run_sources(conn, settings: Settings, rules: Rules, mode: str, days: int, http: Http | None = None,
                sources: list[str] | None = None) -> list[pipeline.RunReport]:
    own = http is None
    http = http or make_http(settings)
    reports = []
    try:
        for name in sources or settings.enabled_sources:
            src = build(name, http, rules)
            if mode == "sweep" and name not in SWEEP_CAPABLE:
                continue  # джерело не вміє повний перегляд вікна
            rep = pipeline.run_source(conn, src, mode, pipeline.window_days(days), rules, settings)
            log.info(rep.summary())
            reports.append(rep)
    finally:
        if own:
            http.close()
    return reports


def housekeeping(conn, settings: Settings, rules: Rules, sender: notify.Sender, quiet: bool = False) -> dict:
    closed = pipeline.refresh_lifecycle(conn)
    reminders = pipeline.make_reminders(conn, rules)
    flushed = notify.flush_events(conn, sender, settings, quiet=quiet)
    alerts = 0 if quiet else notify.alert_health(conn, sender)
    return {"closed": closed, "reminders": reminders, "notified": flushed, "alerts": alerts}


def _last(conn, key: str) -> datetime | None:
    return parse_dt(kv_get(conn, key))


def _mark(conn, key: str, now: datetime) -> None:
    kv_set(conn, key, iso(now))


def tick(conn, settings: Settings, rules: Rules, sender: notify.Sender, now: datetime | None = None,
         http: Http | None = None) -> list[str]:
    """Один такт планувальника: повертає список виконаних задач (для логів і тестів)."""
    now = now or utcnow()
    done: list[str] = []

    last = _last(conn, "job:search")
    if not last or now - last >= timedelta(minutes=settings.incremental_minutes):
        _mark(conn, "job:search", now)
        run_sources(conn, settings, rules, "search", settings.lookback_days, http)
        housekeeping(conn, settings, rules, sender)
        done.append("search")

    w = now.astimezone(WARSAW)
    last = _last(conn, "job:sweep")
    if w.hour >= settings.sweep_hour_warsaw and (not last or now - last >= timedelta(hours=20)) and w.hour < settings.sweep_hour_warsaw + 5:
        _mark(conn, "job:sweep", now)
        run_sources(conn, settings, rules, "sweep", settings.lookback_days, http, sources=[s for s in settings.enabled_sources if s == "ezam"])
        housekeeping(conn, settings, rules, sender)
        done.append("sweep")

    k = now.astimezone(KYIV)
    last_digest = kv_get(conn, "job:digest_date")
    if k.hour >= settings.digest_hour_kyiv and last_digest != k.strftime("%Y-%m-%d"):
        kv_set(conn, "job:digest_date", k.strftime("%Y-%m-%d"))
        text = notify.build_digest(conn, settings, rules)
        sender.send(text)
        done.append("digest")
    return done


def daemon(conn, settings: Settings, rules: Rules, poll_seconds: int = 30) -> None:  # pragma: no cover - нескінченний цикл
    sender = notify.Sender(settings)
    log.info("daemon started; sources=%s", settings.enabled_sources)
    while True:
        try:
            done = tick(conn, settings, rules, sender)
            if done:
                log.info("tick done: %s", done)
        except Exception:  # noqa: BLE001
            log.exception("tick failed")
        time.sleep(poll_seconds)
