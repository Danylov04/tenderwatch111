"""Telegram-сповіщення: нові релевантні тендери, зміни, нагадування про дедлайн, дайджест, збої джерел."""
from __future__ import annotations

import html
import logging
from collections.abc import Callable
from datetime import timedelta

import httpx

from .config import Settings
from .db import jload, kv_get, kv_set
from .health import source_health
from .util import KYIV, WARSAW, parse_dt, trunc, utcnow

log = logging.getLogger("tenderwatch.notify")

IMMEDIATE = {
    "new", "deadline_changed", "lifecycle_changed", "new_notice", "republished", "value_changed", "title_changed",
    "relevance_changed", "deadline_3d", "deadline_1d", "deadline_7d", "deadline_2d",
}
MAX_LEN = 3900
MAX_CARDS = 30

KIND_LABEL = {
    "new": "НОВИЙ ТЕНДЕР",
    "deadline_changed": "ЗМІНЕНО ДЕДЛАЙН",
    "lifecycle_changed": "ЗМІНА СТАТУСУ",
    "new_notice": "НОВЕ ОГОЛОШЕННЯ ПО ТЕНДЕРУ",
    "republished": "ПОВТОРНА ПУБЛІКАЦІЯ",
    "value_changed": "ЗМІНЕНО ВАРТІСТЬ",
    "title_changed": "ЗМІНЕНО НАЗВУ",
    "relevance_changed": "ПІДВИЩЕНО РЕЛЕВАНТНІСТЬ",
}


def _dt(ts: str | None, tz=WARSAW) -> str:
    p = parse_dt(ts)
    return p.astimezone(tz).strftime("%d.%m.%Y %H:%M") if p else "—"


class Sender:
    """Абстракція відправки (у тестах підміняється)."""

    def __init__(self, settings: Settings, post: Callable | None = None):
        self.settings = settings
        self._post = post

    @property
    def enabled(self) -> bool:
        return self.settings.telegram_enabled

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.settings.telegram_token}/sendMessage"
        payload = {"chat_id": self.settings.telegram_chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            if self._post:
                return bool(self._post(url, payload))
            r = httpx.post(url, json=payload, timeout=20)
            if r.status_code != 200:
                log.warning("telegram %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except httpx.HTTPError as e:
            log.warning("telegram error: %s", e)
            return False


def _card(t, kind: str, detail: dict, settings: Settings) -> str:
    esc = html.escape
    label = KIND_LABEL.get(kind, kind.upper())
    if kind.startswith("deadline_") and kind.endswith("d") and kind not in KIND_LABEL:
        label = f"ДЕДЛАЙН ЧЕРЕЗ {kind[9:-1]} дн."
    url = t["url"] or next(iter(jload(t["links"], {}).values()), "")
    title = esc(trunc(t["title"], 300))
    head = f'<a href="{esc(url)}">{title}</a>' if url else title
    lines = [f"<b>{label}</b> · {esc(t['category'] or '—')} · {esc(t['relevance'])}", head]
    buyer = t["buyer"] + (f" ({t['province']})" if t["province"] else "")
    if buyer.strip():
        lines.append(f"Замовник: {esc(buyer)}")
    lines.append(f"Дедлайн: {_dt(t['deadline'])} (Варшава)")
    if t["value_amount"]:
        lines.append(f"Вартість: {t['value_amount']:,.0f} {esc(t['value_currency'] or 'PLN')}".replace(",", " "))
    if kind == "deadline_changed":
        lines.append(f"Було: {_dt(detail.get('old'))} → стало: {_dt(detail.get('new'))}")
    elif kind == "lifecycle_changed":
        lines.append(f"Статус: {esc(str(detail.get('old')))} → {esc(str(detail.get('new')))}")
    elif kind == "new_notice":
        lines.append(f"Тип: {esc(str(detail.get('notice_type')))}")
    elif kind == "republished":
        lines.append(f"Спроба №{detail.get('attempt')}")
    elif kind == "value_changed":
        lines.append(f"Було: {detail.get('old')} → стало: {detail.get('new')}")
    srcs = ", ".join(jload(t["sources"], []))
    lines.append(f"Джерела: {esc(srcs)}")
    if kind == "new":
        reasons = jload(t["reasons"], [])[:3]
        if reasons:
            lines.append("Чому: " + esc("; ".join(reasons)))
    if settings.public_url:
        lines.append(f'<a href="{esc(settings.public_url)}/#t{t["pk"]}">Відкрити в дашборді</a>')
    return "\n".join(lines)


def flush_events(conn, sender: Sender, settings: Settings) -> dict:
    """Розсилає незіслані події. Якщо Telegram вимкнено — позначає їх «пропущеними» (2), щоб не завалити потім."""
    rows = conn.execute(
        """SELECT e.pk AS epk, e.kind, e.detail, t.* FROM events e JOIN tenders t ON t.pk=e.tender_pk
           WHERE e.notified=0 ORDER BY e.pk"""
    ).fetchall()
    sent = skipped = 0
    blocks: list[tuple[int, str]] = []
    for r in rows:
        kind = r["kind"]
        # сповіщаємо лише про релевантні й не «пропущені»; review видно в дашборді та дайджесті
        drop = kind not in IMMEDIATE or r["user_status"] == "skip" or r["relevance"] != "relevant"
        if drop:
            conn.execute("UPDATE events SET notified=2 WHERE pk=?", (r["epk"],))
            skipped += 1
            continue
        blocks.append((r["epk"], _card(r, kind, jload(r["detail"], {}), settings)))
    if not sender.enabled:
        for epk, _ in blocks:
            conn.execute("UPDATE events SET notified=2 WHERE pk=?", (epk,))
        return {"sent": 0, "skipped": skipped + len(blocks), "enabled": False}
    if len(blocks) > MAX_CARDS:
        # масові події (перший запуск, відновлення після простою): один стислий підсумок замість десятків повідомлень
        titles = [ln for _, b in blocks for ln in b.splitlines()[1:2]]
        text = (f"<b>Багато подій одночасно: {len(blocks)}</b>\nДеталі — у дашборді"
                + (f" ({html.escape(settings.public_url)})" if settings.public_url else "") + ".\n\n" + "\n".join(titles[:MAX_CARDS]))
        if sender.send(text[:MAX_LEN]):
            conn.executemany("UPDATE events SET notified=1 WHERE pk=?", [(p,) for p, _ in blocks])
            sent += len(blocks)
        return {"sent": sent, "skipped": skipped, "enabled": True, "summarized": True}
    for chunk_pks, text in _pack(blocks):
        if sender.send(text):
            conn.executemany("UPDATE events SET notified=1 WHERE pk=?", [(p,) for p in chunk_pks])
            sent += len(chunk_pks)
    return {"sent": sent, "skipped": skipped, "enabled": True}


def _pack(blocks: list[tuple[int, str]]):
    cur_pks: list[int] = []
    cur = ""
    for epk, text in blocks:
        if cur and len(cur) + len(text) + 2 > MAX_LEN:
            yield cur_pks, cur
            cur_pks, cur = [], ""
        cur = (cur + "\n\n" + text) if cur else text
        cur_pks.append(epk)
    if cur:
        yield cur_pks, cur


def alert_health(conn, sender: Sender, cooldown_hours: int = 6) -> int:
    """Збої джерел — не частіше ніж раз на cooldown для кожного джерела/режиму."""
    n = 0
    for h in source_health(conn):
        if h["status"] == "ok":
            continue
        key = f"alert:{h['source']}:{h['mode']}"
        last = kv_get(conn, key)
        lp = parse_dt(last)
        if lp and utcnow() - lp < timedelta(hours=cooldown_hours):
            continue
        text = f"<b>ДЖЕРЕЛО {html.escape(h['source'])}/{h['mode']}: {h['status'].upper()}</b>\n" + "\n".join(
            html.escape(p) for p in h["problems"]
        )
        if sender.send(text):
            kv_set(conn, key, utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
            n += 1
    return n


def build_digest(conn, settings: Settings, rules=None) -> str:
    now = utcnow()
    horizon = (now + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    opened = conn.execute(
        """SELECT * FROM tenders WHERE relevance='relevant' AND lifecycle='open' AND user_status!='skip'
           AND deadline IS NOT NULL AND deadline<=? ORDER BY deadline""",
        (horizon,),
    ).fetchall()
    review = conn.execute("SELECT COUNT(*) c FROM tenders WHERE relevance='review' AND lifecycle='open' AND user_status='new'").fetchone()["c"]
    total_open = conn.execute("SELECT COUNT(*) c FROM tenders WHERE relevance='relevant' AND lifecycle='open'").fetchone()["c"]
    lines = [f"<b>Щоденний дайджест · {now.astimezone(KYIV).strftime('%d.%m.%Y')}</b>",
             f"Відкритих релевантних тендерів: {total_open}; на перевірку (review): {review}"]
    if opened:
        lines.append("\n<b>Дедлайни найближчих 14 днів:</b>")
        for t in opened[:25]:
            st = {"new": "", "watch": " [стежу]", "applied": " [подано]"}.get(t["user_status"], "")
            lines.append(f"{_dt(t['deadline'], KYIV)} — {html.escape(trunc(t['title'], 110))} ({html.escape(t['buyer'] or '?')}){st}")
    bad = [h for h in source_health(conn) if h["status"] != "ok"]
    if bad:
        lines.append("\n<b>Проблеми з джерелами:</b>")
        for h in bad:
            lines.append(f"{html.escape(h['source'])}/{h['mode']}: {html.escape('; '.join(h['problems']))}")
    text = "\n".join(lines)
    if rules is not None and now.astimezone(KYIV).weekday() == 0:  # понеділок — плюс звіт про повноту
        from . import recall as _recall

        rep = _recall.recall_report(conn, rules)
        text += (f"\n\n<b>Повнота (тиждень)</b>\nЗнайдено релевантних: {rep['union']}, "
                 f"оцінка загалом ≈{rep['estimated_total']}, ймовірно непомічених ≈{rep['estimated_unseen']}")
        missing = [c["id"] for c in rep.get("canaries", []) if not c["found"]]
        if missing:
            text += f"\nКАНАРКИ НЕ ЗНАЙДЕНО: {', '.join(missing)}"
    return text
