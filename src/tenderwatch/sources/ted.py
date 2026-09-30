"""TED (Tenders Electronic Daily) — офіційне Search API v3 (POST, перевірено 2026-09-20).

POST https://api.ted.europa.eu/v3/notices/search
  {"query": "buyer-country=POL AND (classification-cpv=45216129 OR ...) AND publication-date>=20260801",
   "fields": [...], "page": 1, "limit": 100, "scope": "ALL", "paginationMode": "PAGE_NUMBER"}
Відповідь: {"notices": [...], "totalNoticeCount": N, "iterationNextToken": ..., "timedOut": bool}.
Назва — словник мов; польська має вигляд «Polska – <мітка CPV> – <справжня назва>».
"""
from __future__ import annotations

import re
from collections.abc import Iterator

from ..models import AWARDED, CLOSED, OPEN, PLANNED, UNKNOWN, Notice
from ..util import clean, iso, parse_dt, utcnow
from .base import Source, SourceError, Window, check_filter

API = "https://api.ted.europa.eu/v3/notices/search"
LIMIT = 100
MAX_PAGES = 50
FIELDS = [
    "publication-number", "notice-title", "buyer-name", "publication-date", "deadline-receipt-tender-date-lot",
    "classification-cpv", "notice-type",
]


def _pick_lang(d, prefer=("pol", "eng")) -> str:
    if isinstance(d, str):
        return clean(d)
    if isinstance(d, list):
        return clean(d[0]) if d else ""
    if isinstance(d, dict):
        for lang in prefer:
            v = d.get(lang)
            if v:
                return _pick_lang(v)
        for v in d.values():
            if v:
                return _pick_lang(v)
    return ""


def _title(d) -> str:
    full = _pick_lang(d)
    parts = full.split(" – ", 2)
    return clean(parts[2]) if len(parts) == 3 else full


def _lifecycle(ntype: str, deadline: str | None) -> str:
    t = (ntype or "").lower()
    if t.startswith("cn"):
        return CLOSED if deadline and deadline < iso(utcnow()) else OPEN
    if t.startswith("can"):
        return AWARDED
    if t.startswith("pin"):
        return PLANNED
    return UNKNOWN


def to_notice(item: dict, strategy: str) -> Notice:
    pub_no = item.get("publication-number") or ""
    dls = item.get("deadline-receipt-tender-date-lot") or []
    dl = None
    for d in (dls if isinstance(dls, list) else [dls]):
        p = parse_dt(d)
        if p:
            dl = iso(p)
            break
    cpv = list(dict.fromkeys(re.sub(r"\D", "", c)[:8] for c in (item.get("classification-cpv") or [])))
    links = item.get("links") or {}
    html = links.get("html") or {}
    pub = parse_dt(item.get("publication-date"))
    ntype = item.get("notice-type") or ""
    return Notice(
        source="ted",
        source_id=pub_no,
        title=_title(item.get("notice-title")),
        strategy=strategy,
        keys=[f"ted:{pub_no}"] if pub_no else [],
        notice_type=ntype,
        lifecycle=_lifecycle(ntype, dl),
        published_at=iso(pub) if pub else None,
        deadline=dl,
        url=html.get("POL") or html.get("ENG") or (f"https://ted.europa.eu/en/notice/-/detail/{pub_no}" if pub_no else ""),
        buyer=_pick_lang(item.get("buyer-name")),
        cpv=[c for c in cpv if c],
        raw={k: v for k, v in item.items() if k not in ("links",)},
    )


class TedSource(Source):
    name = "ted"

    def _query(self, window: Window) -> str:
        # ВАЖЛИВО: список кодів пишемо через `classification-cpv IN (A B C ...)`, а НЕ через
        # ланцюжок `classification-cpv=A OR classification-cpv=B OR ...` у дужках. Останнє на практиці
        # (2026-09-23..24, багато годинних прогонів поспіль) валило guard check_filter — 4/5..19/35 записів
        # не мали жодного з очікуваних CPV, що вказує на неправильний парсинг дужок/OR у TED expert-query
        # (найімовірніше: AND зв'язується лише з першим OR-членом, і CPV-фільтр фактично ігнорується).
        # `IN (...)` — задокументований робочий синтаксис для списку значень одного поля.
        cpvs = " ".join(self.rules.cpv_codes)
        return f"buyer-country=POL AND classification-cpv IN ({cpvs}) AND publication-date>={window.start.strftime('%Y%m%d')}"

    def search(self, window: Window) -> Iterator[Notice]:
        query = self._query(window)
        wanted = set(self.rules.cpv_codes)
        got = 0
        for page in range(1, MAX_PAGES + 1):
            data = self.http.post_json(
                API,
                {"query": query, "fields": FIELDS, "page": page, "limit": LIMIT, "scope": "ALL", "paginationMode": "PAGE_NUMBER"},
            )
            self.stats.pages += 1
            if not isinstance(data, dict) or "notices" not in data:
                raise SourceError("TED: у відповіді нема ключа 'notices' — схема змінилась?")
            if data.get("timedOut"):
                self.stats.warnings.append("TED: timedOut=true, результат може бути неповним")
            items = data["notices"]
            if not items:
                return
            check_filter(
                items,
                lambda x: bool(wanted & {re.sub(r"\D", "", c)[:8] for c in (x.get("classification-cpv") or [])}),
                "TED classification-cpv",
            )
            for it in items:
                self.stats.count("cpv")
                yield to_notice(it, "cpv")
            got += len(items)
            total = data.get("totalNoticeCount")
            if isinstance(total, int) and got >= total:
                return
            if len(items) < LIMIT:
                return

    def doctor(self) -> list[tuple[str, bool, str]]:
        try:
            data = self.http.post_json(
                API,
                {"query": "buyer-country=POL AND classification-cpv=45216129", "fields": FIELDS, "page": 1, "limit": 3,
                 "scope": "ALL", "paginationMode": "PAGE_NUMBER"},
            )
            ok = isinstance(data, dict) and "notices" in data
            return [("ted search v3", ok, f"totalNoticeCount={data.get('totalNoticeCount')}" if ok else "нема 'notices'")]
        except Exception as e:  # noqa: BLE001
            return [("ted search v3", False, str(e))]
