"""Оркестрація: джерело -> класифікація -> (тіло) -> БД -> події."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import store
from .classify import classify, needs_body_scan
from .config import Rules, Settings
from .db import add_event, jdump, jload
from .models import OPEN
from .sources import Source, Window
from .util import now_iso, utcnow

log = logging.getLogger("tenderwatch.pipeline")

SEARCH_MODES = ("search", "sweep", "backfill")


@dataclass
class RunReport:
    source: str
    mode: str
    ok: bool = False
    fetched: int = 0
    relevant: int = 0
    new_tenders: int = 0
    events: dict[str, int] = field(default_factory=dict)
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    bodies: int = 0

    def summary(self) -> str:
        state = "OK" if self.ok else f"ПОМИЛКА: {self.error}"
        return (f"{self.source}/{self.mode}: {state}; отримано {self.fetched}, релевантних {self.relevant}, "
                f"нових тендерів {self.new_tenders}, тіл {self.bodies}")


def window_days(days: int, end: datetime | None = None) -> Window:
    end = end or utcnow()
    return Window(end - timedelta(days=days), end)


def process_notice(conn, src: Source, n, rules: Rules, settings: Settings, mode: str, rep: RunReport):
    """Один запис джерела: класифікація, за потреби тіло, запис."""
    cls = classify(n, rules)
    from_search = not n.strategy.startswith("sweep")
    body_scanned = False

    want_body = False
    if src.name in ("ezam", "pz"):
        if cls.relevance in ("relevant", "review") and n.lifecycle in ("open", "unknown", "planned"):
            want_body = True
        elif not from_search and settings.body_scan and needs_body_scan(n, cls, rules):
            want_body = True
        if want_body and store.already_scanned(conn, n.source, n.source_id):
            want_body = False
    if want_body:
        src.enrich(n)
        rep.bodies += 1
        body_scanned = True
        cls = classify(n, rules)

    res = store.ingest(conn, n, cls, persist_noise=from_search, body_scanned=body_scanned)
    if cls.relevance != "noise":
        rep.relevant += 1
    if res.created:
        rep.new_tenders += 1
    for e in res.events:
        rep.events[e] = rep.events.get(e, 0) + 1
    return res


def run_source(conn, src: Source, mode: str, window: Window, rules: Rules, settings: Settings) -> RunReport:
    """mode: 'search' (швидко, ключові слова/CPV), 'sweep' (повний перегляд вікна), 'backfill' (обидва по великому вікну)."""
    rep = RunReport(src.name, mode)
    started = now_iso()
    run_pk = conn.execute(
        "INSERT INTO runs(source, mode, started_at) VALUES(?,?,?)", (src.name, mode, started)
    ).lastrowid
    try:
        streams = []
        if mode in ("search", "backfill"):
            streams.append(src.search(window))
        if mode in ("sweep", "backfill"):
            streams.append(src.sweep(window))
        for stream in streams:
            for n in stream:
                if not n.source_id:
                    rep.warnings.append(f"запис без source_id: {n.title[:60]}")
                    continue
                process_notice(conn, src, n, rules, settings, mode, rep)
        rep.fetched = src.stats.fetched
        rep.warnings += src.stats.warnings
        if src.stats.unknown_types:
            rep.warnings.append(f"невідомі типи оголошень: {sorted(src.stats.unknown_types)}")
        rep.ok = True
    except Exception as e:  # noqa: BLE001 — будь-яка помилка джерела не повинна ламати інші джерела
        rep.ok = False
        rep.error = f"{type(e).__name__}: {e}"
        rep.fetched = src.stats.fetched
        log.exception("source %s failed", src.name)
    finally:
        conn.execute(
            """UPDATE runs SET finished_at=?, ok=?, fetched=?, relevant=?, new_tenders=?, error=?, detail=? WHERE pk=?""",
            (now_iso(), int(rep.ok), rep.fetched, rep.relevant, rep.new_tenders, rep.error,
             jdump({"events": rep.events, "warnings": rep.warnings[:20], "pages": src.stats.pages,
                    "strategies": src.stats.strategies, "bodies": rep.bodies}), run_pk),
        )
    return rep


# ---------- обслуговування ----------

def refresh_lifecycle(conn) -> int:
    """Відкриті тендери з простроченим дедлайном -> closed (тиха подія)."""
    now = now_iso()
    rows = conn.execute("SELECT pk FROM tenders WHERE lifecycle=? AND deadline IS NOT NULL AND deadline < ?", (OPEN, now)).fetchall()
    for r in rows:
        conn.execute("UPDATE tenders SET lifecycle='closed', last_changed=? WHERE pk=?", (now, r["pk"]))
        pk_ev = add_event(conn, r["pk"], "deadline_passed", {})
        conn.execute("UPDATE events SET notified=2 WHERE pk=?", (pk_ev,))
    return len(rows)


def make_reminders(conn, rules: Rules) -> int:
    """Події-нагадування «до дедлайну N днів» (по одній на тендер+дедлайн+N)."""
    now = utcnow()
    created = 0
    rows = conn.execute(
        "SELECT pk, deadline FROM tenders WHERE lifecycle='open' AND relevance='relevant' AND user_status!='skip' AND deadline IS NOT NULL"
    ).fetchall()
    for r in rows:
        dl = datetime.fromisoformat(r["deadline"].replace("Z", "+00:00"))
        left = dl - now
        for d in sorted(rules.reminder_days):
            if timedelta(0) < left <= timedelta(days=d):
                kind = f"deadline_{d}d"
                exists = conn.execute(
                    "SELECT 1 FROM events WHERE tender_pk=? AND kind=? AND detail LIKE ?",
                    (r["pk"], kind, f'%"{r["deadline"]}"%'),
                ).fetchone()
                if not exists:
                    add_event(conn, r["pk"], kind, {"deadline": r["deadline"]})
                    created += 1
                break  # лише найближчий поріг
    return created


def load_tender(conn, pk: int) -> dict | None:
    r = conn.execute("SELECT * FROM tenders WHERE pk=?", (pk,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for col in ("tags", "reasons", "cpv_all", "sources", "contractors", "links"):
        d[col] = jload(d[col], [] if col != "links" else {})
    return d
