"""bazakonkurencyjnosci.gov.pl — оголошення за «zasada konkurencyjności» (проєкти з ЄС-співфінансуванням).

GET /api/announcements/search?title=<слово>. Схема цього API мною НЕ підтверджена на живих даних
(під час розробки ендпоїнт віддавав 500), тому розбір максимально терпимий, а `doctor` показує реальний стан.
Для програми укриттів джерело історично давало 0 записів (бюджетне фінансування), але воно дешеве й може ожити.
"""
from __future__ import annotations

from collections.abc import Iterator

from ..http import HttpError
from ..models import CLOSED, OPEN, Notice
from ..util import clean, fold, iso, parse_dt, utcnow
from .base import Source, SourceError, Window, check_filter

BASE = "https://bazakonkurencyjnosci.gov.pl"
PAGE = 20
MAX_PAGES = 10


def _pick(d: dict, *names):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return None


def _items(data) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("content", "items", "data", "results", "announcements", "list"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    raise SourceError("bazakonkurencyjnosci: невідома форма відповіді (нема списку записів)")


def to_notice(item: dict, strategy: str) -> Notice:
    bid = str(_pick(item, "id", "announcementId", "number", "uuid") or "")
    title = clean(str(_pick(item, "title", "name", "subject", "orderObject") or ""))
    buyer = clean(str(_pick(item, "contractorName", "buyer", "organization", "organizationName", "author") or ""))
    dl = parse_dt(_pick(item, "offerDeadline", "submissionDeadline", "deadline", "offersDeadline", "submittingOffersDate"))
    pub = parse_dt(_pick(item, "publicationDate", "publishDate", "createdAt", "dateOfPublication"))
    dl_iso = iso(dl) if dl else None
    return Notice(
        source="bk",
        source_id=bid,
        title=title,
        strategy=strategy,
        keys=[f"bk:{bid}"] if bid else [],
        notice_type="zasada konkurencyjności",
        lifecycle=CLOSED if (dl_iso and dl_iso < iso(utcnow())) else OPEN,
        published_at=iso(pub) if pub else None,
        deadline=dl_iso,
        url=f"{BASE}/announcements/{bid}" if bid else BASE,
        buyer=buyer,
        raw=item,
    )


class BkSource(Source):
    name = "bk"

    def search(self, window: Window) -> Iterator[Notice]:
        for kw in self.rules.pz_keywords:
            strategy = f"kw:{kw}"
            seen: set[str] = set()
            for page in range(0, MAX_PAGES):
                try:
                    data = self.http.get_json(f"{BASE}/api/announcements/search", params={"title": kw, "page": page, "size": PAGE})
                except HttpError as e:
                    raise SourceError(f"bazakonkurencyjnosci: {e}") from e
                self.stats.pages += 1
                items = _items(data)
                folded = fold(kw)
                check_filter(items, lambda x, f=folded: f in fold(str(_pick(x, "title", "name", "subject", "orderObject") or "")), f"title={kw}", 0.4)
                fresh = [i for i in items if str(_pick(i, "id", "announcementId", "number", "uuid")) not in seen]
                if not fresh:
                    break
                for it in fresh:
                    n = to_notice(it, strategy)
                    seen.add(n.source_id)
                    self.stats.count(strategy)
                    yield n
                if len(items) < PAGE:
                    break

    def doctor(self) -> list[tuple[str, bool, str]]:
        try:
            data = self.http.get_json(f"{BASE}/api/announcements/search", params={"title": "modułow", "page": 0, "size": 5})
            items = _items(data)
            return [("bk search", True, f"{len(items)} записів (схема розпізнана)")]
        except Exception as e:  # noqa: BLE001
            return [("bk search", False, str(e))]
