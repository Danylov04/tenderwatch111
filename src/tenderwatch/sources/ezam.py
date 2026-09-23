"""e-Zamówienia (BZP) — головне джерело.

Єдиний ендпоїнт `mo-board/api/v1/Board/Search` (перевірено на живих відповідях 2026-09-20):
  * OrderObject=<підрядок назви>            — пошук по назві (фільтр працює)
  * CpvCode=<8 цифр>                        — точний CPV-фільтр (за основним і додатковими кодами)
  * NoticeType=<тип>                        — тип оголошення (ContractNotice, TenderResultNotice, ...)
  * PublicationDateFrom / PublicationDateTo — діапазон дат публікації (yyyy-mm-dd)
  * SortingColumnName=PublicationDate & SortingDirection=DESC|ASC, PageNumber, PageSize (макс. 10)
Невідомі параметри сервер МОВЧКИ ІГНОРУЄ (наприклад TenderId) — тому кожна відповідь проходить `check_filter`.
Деталі: `GetNoticeHtmlBodyById?noticeId=<objectId>` (саме objectId, не noticeNumber).
Відповідь Search — голий JSON-масив.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import timedelta

from ..http import HttpError
from ..models import AWARDED, CANCELLED, CLOSED, OPEN, UNKNOWN, Notice
from ..util import WARSAW, clean, fold, iso, parse_dt, utcnow
from .base import Source, SourceError, Window, check_filter
from .ezam_body import parse_body

log = logging.getLogger("tenderwatch.ezam")

BASE = "https://ezamowienia.gov.pl/mo-board/api/v1/Board/"
PAGE = 10
MAX_PAGES = 300          # запобіжник для пошуку (слова/CPV)
MAX_PAGES_SWEEP = 3000   # sweep усіх оголошень за вікно дат

# TERYT × 2 -> воєводство (BZP кодує organizationProvince як PL + код TERYT)
PROVINCES = {
    "PL02": "dolnośląskie", "PL04": "kujawsko-pomorskie", "PL06": "lubelskie", "PL08": "lubuskie",
    "PL10": "łódzkie", "PL12": "małopolskie", "PL14": "mazowieckie", "PL16": "opolskie",
    "PL18": "podkarpackie", "PL20": "podlaskie", "PL22": "pomorskie", "PL24": "śląskie",
    "PL26": "świętokrzyskie", "PL28": "warmińsko-mazurskie", "PL30": "wielkopolskie", "PL32": "zachodniopomorskie",
}

_CPV_RX = re.compile(r"(\d{8})-\d\s*\(([^)]*)\)")


def parse_cpv(raw: str | None) -> tuple[list[str], dict[str, str]]:
    codes, labels = [], {}
    for code, label in _CPV_RX.findall(raw or ""):
        if code not in labels:
            codes.append(code)
            labels[code] = clean(label)
    if not codes:  # запасний варіант: просто 8-цифрові коди
        codes = list(dict.fromkeys(re.findall(r"\b(\d{8})\b", raw or "")))
    return codes, labels


def _kind(order_type: str | None) -> str:
    t = (order_type or "").lower()
    if t.startswith("work"):
        return "works"
    if t.startswith("serv"):
        return "services"
    if t.startswith(("suppl", "deliver")):
        return "supplies"
    return ""


def _lifecycle(notice_type: str, procedure_result: str | None, deadline_iso: str | None) -> str:
    nt = notice_type or ""
    if nt == "TenderResultNotice":
        pr = fold(procedure_result)
        if "uniewa" in pr or "niezawar" in pr or "anul" in pr:
            return CANCELLED
        if "zawarc" in pr:
            return AWARDED
        return CLOSED
    if nt == "ContractPerformingNotice":
        return AWARDED
    if nt == "ContractNotice" or re.search(r"change|amend|modif|correct|zmian", nt, re.I):
        if deadline_iso and deadline_iso < iso(utcnow()):
            return CLOSED
        return OPEN
    return UNKNOWN


def to_notice(item: dict, strategy: str) -> Notice:
    tender_id = item.get("tenderId") or ""
    bzp = item.get("bzpNumber") or ""
    dl = parse_dt(item.get("submittingOffersDate"))
    deadline = iso(dl) if dl else None
    cpv, labels = parse_cpv(item.get("cpvCode"))
    keys = []
    if tender_id:
        keys.append(f"ezam:{tender_id}")
    if bzp:
        keys.append(f"bzp:{bzp}")
    ntype = item.get("noticeType") or ""
    published = parse_dt(item.get("publicationDate"))
    return Notice(
        source="ezam",
        source_id=item.get("objectId") or item.get("noticeNumber") or "",
        title=clean(item.get("orderObject")),
        strategy=strategy,
        keys=keys,
        notice_type=ntype,
        lifecycle=_lifecycle(ntype, item.get("procedureResult"), deadline),
        published_at=iso(published) if published else None,
        deadline=deadline,
        url=f"https://ezamowienia.gov.pl/mp-client/search/list/{tender_id}" if tender_id else "",
        buyer=clean(item.get("organizationName")),
        buyer_nip=clean(item.get("organizationNationalId")),
        province=PROVINCES.get(item.get("organizationProvince") or "", item.get("organizationProvince") or ""),
        city=clean(item.get("organizationCity")),
        kind=_kind(item.get("orderType")),
        cpv=cpv,
        cpv_labels=labels,
        contractors=[
            {"name": clean(c.get("contractorName")), "city": clean(c.get("contractorCity")),
             "nip": clean(c.get("contractorNationalId"))}
            for c in (item.get("contractors") or [])
        ],
        extra={"notice_number": item.get("noticeNumber"), "bzp": bzp, "procedure_result": item.get("procedureResult")},
        raw={k: v for k, v in item.items() if k not in ("htmlBody",)},
    )


class EzamSource(Source):
    name = "ezam"

    # ---- низькорівневе ----
    def _page(self, params: dict) -> list[dict]:
        data = self.http.get_json(BASE + "Search", params=params)
        self.stats.pages += 1
        if not isinstance(data, list):
            raise SourceError(f"очікувався JSON-масив, отримано {type(data).__name__}: схема змінилась?")
        for it in data:
            nt = it.get("noticeType")
            if nt and nt not in ("ContractNotice", "TenderResultNotice", "ContractPerformingNotice"):
                self.stats.unknown_types.add(nt)
        return data

    def _walk(self, params: dict, since_iso: str | None, guard=None, what: str = "", max_pages: int | None = None) -> Iterator[dict]:
        """Гортає сторінки за DESC-сортуванням; зупиняється, коли записи старші за since (якщо задано).

        Якщо ліміт сторінок вичерпано, а кінця ще нема — це ПОМИЛКА (результат неповний), а не тихе усікання.
        """
        max_pages = max_pages or MAX_PAGES
        base = {"SortingColumnName": "PublicationDate", "SortingDirection": "DESC", "PageSize": PAGE, **params}
        for page in range(1, max_pages + 1):
            items = self._page({**base, "PageNumber": page})
            if not items:
                return
            if guard and page == 1:
                check_filter(items, guard, what)
            elif guard:
                check_filter(items, guard, what, tolerance=0.25)
            stop = False
            for it in items:
                pub = iso(parse_dt(it.get("publicationDate"))) if parse_dt(it.get("publicationDate")) else None
                if since_iso and pub and pub < since_iso and params.get("SortingDirection", "DESC") == "DESC":
                    stop = True
                    continue
                yield it
            if stop or len(items) < PAGE:
                return
        raise SourceError(f"{what}: досягнуто ліміт {max_pages} сторінок, результат неповний — збільшіть ліміт або звузьте вікно")

    # ---- стратегії ----
    def search(self, window: Window) -> Iterator[Notice]:
        since = iso(window.start)
        for kw in self.rules.title_keywords:
            strategy = f"title:{kw}"
            folded = fold(kw)
            for it in self._walk({"OrderObject": kw}, since, lambda x, f=folded: f in fold(x.get("orderObject")), f"OrderObject={kw}"):
                self.stats.count(strategy)
                yield to_notice(it, strategy)
        for code in self.rules.cpv_codes:
            strategy = f"cpv:{code}"
            for it in self._walk({"CpvCode": code}, since, lambda x, c=code: c in (x.get("cpvCode") or ""), f"CpvCode={code}"):
                self.stats.count(strategy)
                yield to_notice(it, strategy)

    def sweep(self, window: Window) -> Iterator[Notice]:
        """Усі оголошення (усіх типів) за діапазон дат — лікує «шукали лише по назві».

        Йдемо ПО ДНЯХ: кожен день — окремий запит з From=To=день. Так довгий backfill не впирається в ліміт сторінок,
        видно прогрес у логах, а збій одного дня не губить решту. Guard приймає ±1 добу навколо дня (межі доби
        в API можуть рахуватися за Варшавою), його мета — відловити ситуацію, коли фільтр дат узагалі ігнорується.
        """
        day, last = window.start.date(), window.end.date()
        while day <= last:
            d = day.isoformat()
            base = parse_dt(d)
            lo = iso(base - timedelta(days=1)) if base else None
            hi = iso(base + timedelta(days=2)) if base else None

            def in_window(x, lo=lo, hi=hi):
                p = parse_dt(x.get("publicationDate"))
                return bool(p and lo and hi and lo <= iso(p) < hi)

            n = 0
            for it in self._walk({"PublicationDateFrom": d, "PublicationDateTo": d}, None, in_window,
                                 f"PublicationDate {d}", max_pages=MAX_PAGES_SWEEP):
                n += 1
                self.stats.count("sweep")
                yield to_notice(it, "sweep")
            log.info("sweep %s: %d оголошень", d, n)
            day += timedelta(days=1)

    # ---- деталі ----
    def enrich(self, n: Notice) -> Notice:
        object_id = n.source_id
        try:
            resp = self.http.get(BASE + "GetNoticeHtmlBodyById", params={"noticeId": object_id}, ok=(200, 404))
        except HttpError as e:
            log.warning("body fetch failed for %s: %s", object_id, e)
            self.stats.warnings.append(f"body:{object_id}:{e}")
            return n
        if resp.status_code == 404:
            return n
        b = parse_body(resp.text)
        n.body_text = b["text"]
        n.description = b.get("description") or n.description
        n.reference = b.get("reference") or n.reference
        n.contact_email = b.get("contact_email") or n.contact_email
        if b.get("cpv"):
            n.cpv = list(dict.fromkeys(n.cpv + b["cpv"]))
        if b.get("value_amount") is not None:
            n.value_amount = b["value_amount"]
            n.value_currency = b.get("value_currency", "PLN")
        if b.get("lots"):
            n.lots = b["lots"]
        if not n.deadline and b.get("deadline_text"):
            dl = parse_dt(b["deadline_text"].replace(" ", "T"), default_tz=WARSAW)
            if dl:
                n.deadline = iso(dl)
        n.extra["eu_funded"] = b.get("eu_funded")
        n.extra["plan_number"] = b.get("plan_number")
        return n

    def doctor(self) -> list[tuple[str, bool, str]]:
        out = []
        try:
            items = self._page({"OrderObject": "schron", "SortingColumnName": "PublicationDate", "SortingDirection": "DESC", "PageNumber": 1, "PageSize": PAGE})
            check_filter(items, lambda x: "schron" in fold(x.get("orderObject")), "OrderObject=schron")
            out.append(("ezam OrderObject", True, f"{len(items)} записів, фільтр працює"))
            need = {"objectId", "tenderId", "noticeType", "orderObject", "cpvCode", "publicationDate", "organizationName"}
            missing = need - set(items[0].keys()) if items else set()
            out.append(("ezam схема", not missing, "ok" if not missing else f"немає полів: {sorted(missing)}"))
        except Exception as e:  # noqa: BLE001
            out.append(("ezam OrderObject", False, str(e)))
        try:
            items = self._page({"CpvCode": "45216129", "SortingColumnName": "PublicationDate", "SortingDirection": "DESC", "PageNumber": 1, "PageSize": PAGE})
            check_filter(items, lambda x: "45216129" in (x.get("cpvCode") or ""), "CpvCode=45216129")
            out.append(("ezam CpvCode", True, f"{len(items)} записів, фільтр працює"))
        except Exception as e:  # noqa: BLE001
            out.append(("ezam CpvCode", False, str(e)))
        try:
            end = utcnow()
            w = Window(end - timedelta(days=1), end)
            first = next(iter(self.sweep(w)), None)
            out.append(("ezam sweep за датами", first is not None, "є записи за останню добу" if first else "порожньо (вихідний?)"))
        except Exception as e:  # noqa: BLE001
            out.append(("ezam sweep за датами", False, str(e)))
        return out
