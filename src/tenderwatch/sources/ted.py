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
from datetime import timedelta

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
        # `IN (...)` — задокументований робочий синтаксис для списку значень одного поля (підтверджено
        # зовнішнім джерелом: TED Expert Search, приклад `buyer-country IN (ROU DEU FRA)`, значення
        # через пробіл, без коми).
        #
        # 2026-10-24: навіть з `IN (...)` check_filter далі падає (0/2 відповідних CPV) — запит доходить
        # (200 OK), але фільтр схоже знову ІГНОРУЄТЬСЯ сервером. Це той самий клас бага, що й з OR-ланцюжком:
        # TED expert-query, судячи з усього, чутливий до групування трьох AND-умов без явних дужок навколо
        # КОЖНОЇ з них (не тільки навколо значень IN) — додаємо дужки на кожну умову. НЕ підтверджено
        # наживо з цього середовища (TED API — POST з JSON body, WebFetch такого не вміє) — перевірте
        # наступним логом search/sweep; якщо однакова помилка повториться — проблема не в групуванні,
        # і варто спробувати `doctor()` з `checkQuerySyntax: true`, щоб TED сам сказав, що саме не так.
        cpvs = " ".join(self.rules.cpv_codes)
        return (
            f"(buyer-country=POL) AND (classification-cpv IN ({cpvs})) "
            f"AND (publication-date>={window.start.strftime('%Y%m%d')})"
        )

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
        results: list[tuple[str, bool, str]] = []
        try:
            data = self.http.post_json(
                API,
                {"query": "buyer-country=POL AND classification-cpv=45216129", "fields": FIELDS, "page": 1, "limit": 3,
                 "scope": "ALL", "paginationMode": "PAGE_NUMBER"},
            )
            ok = isinstance(data, dict) and "notices" in data
            results.append(
                ("ted search v3", ok, f"totalNoticeCount={data.get('totalNoticeCount')}" if ok else "нема 'notices'")
            )
        except Exception as e:  # noqa: BLE001
            results.append(("ted search v3", False, str(e)))

        # Окрема перевірка саме нашого продакшн-запиту (`_query()` з IN + дужками) — чи CPV-фільтр
        # СПРАВДІ фільтрує, а не повертає 200 OK і мовчки ігнорує умову (саме так виглядав і старий
        # OR-ланцюжковий баг, і — 2026-10-24 — цей самий `IN (...)` без дужок навколо кожної умови, і
        # З ДУЖКАМИ — досі так само: 0/2 на прод-логу). При незбігу друкуємо СИРІ classification-cpv
        # невідповідних записів — щоб видно було, чи фільтр взагалі щось робить, чи повертає геть
        # випадкові CPV (отже сервер його просто не бачить/ігнорує).
        try:
            window = Window(start=utcnow() - timedelta(days=30), end=utcnow())
            query = self._query(window)
            data = self.http.post_json(
                API,
                {"query": query, "fields": FIELDS, "page": 1, "limit": 20, "scope": "ALL", "paginationMode": "PAGE_NUMBER"},
            )
            items = data.get("notices") or [] if isinstance(data, dict) else []
            wanted = set(self.rules.cpv_codes)
            mismatched_cpv = [
                it.get("classification-cpv") for it in items
                if not (wanted & {re.sub(r"\D", "", c)[:8] for c in (it.get("classification-cpv") or [])})
            ]
            matched = len(items) - len(mismatched_cpv)
            ok = bool(items) and not mismatched_cpv
            if items:
                detail = f"query={query!r}; {matched}/{len(items)} відповідають; CPV невідповідних (до 5): {mismatched_cpv[:5]!r}"
            else:
                detail = f"query={query!r}; 0 записів за 30 днів (замало даних для перевірки)"
            results.append(("ted cpv-filter (продакшн-запит)", ok, detail))
        except Exception as e:  # noqa: BLE001
            results.append(("ted cpv-filter (продакшн-запит)", False, str(e)))

        # checkQuerySyntax: якщо TED API підтримує цей прапорець (зустрічається в описах стороннього
        # використання v3 API), він має сказати, чи запит ВАЛІДНИЙ, без потреби вгадувати по поведінці.
        # Якщо прапорець просто ігнорується серверу (що теж можливо) — цей рядок хоча б підтвердить, що
        # відповідь на звичний запит і на запит з прапорцем ідентичні, і треба інший спосіб діагностики.
        try:
            query = self._query(Window(start=utcnow() - timedelta(days=30), end=utcnow()))
            data = self.http.post_json(
                API,
                {"query": query, "fields": FIELDS, "page": 1, "limit": 1, "scope": "ALL",
                 "paginationMode": "PAGE_NUMBER", "checkQuerySyntax": True},
            )
            results.append(("ted checkQuerySyntax", True, f"відповідь сервера: {data!r}"[:500]))
        except Exception as e:  # noqa: BLE001
            results.append(("ted checkQuerySyntax", False, str(e)))
        return results
