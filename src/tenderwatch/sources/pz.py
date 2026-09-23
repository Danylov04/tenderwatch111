"""platformazakupowa.pl — платформа, де гміни публікують і підпорогові запити (poza BZP).

GET /all?page=N&limit=..&query=<слово>  -> HTML. Кожен запис: `div.auction-row`,
  a.auction-title[href=/transakcja/<ID>] (у тексті наприкінці «(ID 1374414)»),
  текст замовника — вузол після <br> у `.product-info`,
  дедлайн — <b id="countdown-<ID>" title="22-09-2026 10:00:00 Europe/Warsaw">.
Запити тільки послідовні й повільні: паралельні дають 429.
"""
from __future__ import annotations

import re
from collections.abc import Iterator

from bs4 import BeautifulSoup, Tag

from ..http import HttpError
from ..models import CLOSED, OPEN, Notice
from ..util import clean, iso, parse_dt, utcnow
from .base import Source, SourceError, Window

BASE = "https://platformazakupowa.pl"
MAX_PAGES = 15
_ID_RX = re.compile(r"\(ID\s*(\d+)\)\s*$")
BZP_RX = re.compile(r"\b(\d{4}/BZP\s?\d{8})\b")
OCDS_RX = re.compile(r"\b(ocds-[0-9a-z]{6}-[0-9a-f-]{36})\b")


def parse_listing(html: str, strategy: str) -> list[Notice]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select(".auction-row")
    if not rows and "auction" not in html:
        raise SourceError("platformazakupowa: не знайдено жодного .auction-row — верстка змінилась або відповідь порожня")
    out: list[Notice] = []
    for row in rows:
        a = row.select_one("a.auction-title") or row.select_one("a[href*='/transakcja/']")
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"/transakcja/(\d+)", href)
        if not m:
            continue
        pz_id = m.group(1)
        title = clean(a.get_text(" "))
        title = _ID_RX.sub("", title).strip()
        buyer = ""
        info = row.select_one(".product-info")
        if info:
            for s in info.find_all(string=True, recursive=False):
                t = clean(str(s))
                if t and not re.search(r"proceedings|postępowanie|current", t, re.I):
                    buyer = t
                    break
        b = row.select_one("b[id^=countdown-]")
        dl = parse_dt(b.get("title") if isinstance(b, Tag) else None)
        dl_iso = iso(dl) if dl else None
        lifecycle = CLOSED if (dl_iso and dl_iso < iso(utcnow())) else OPEN
        out.append(
            Notice(
                source="pz",
                source_id=pz_id,
                title=title,
                strategy=strategy,
                keys=[f"pz:{pz_id}"],
                notice_type="zapytanie/postępowanie",
                lifecycle=lifecycle,
                deadline=dl_iso,
                url=f"{BASE}/transakcja/{pz_id}",
                buyer=buyer,
                raw={"id": pz_id, "title": title, "buyer": buyer, "deadline": b.get("title") if isinstance(b, Tag) else None},
            )
        )
    return out


class PzSource(Source):
    name = "pz"

    def search(self, window: Window) -> Iterator[Notice]:
        for kw in self.rules.pz_keywords:
            strategy = f"kw:{kw}"
            seen: set[str] = set()
            for page in range(1, MAX_PAGES + 1):
                resp = self.http.get(f"{BASE}/all", params={"page": page, "limit": 50, "query": kw})
                self.stats.pages += 1
                items = parse_listing(resp.text, strategy)
                fresh = [n for n in items if n.source_id not in seen]
                if not fresh:
                    break
                for n in fresh:
                    seen.add(n.source_id)
                    self.stats.count(strategy)
                    yield n

    def enrich(self, n: Notice) -> Notice:
        """Шукає на сторінці процедури номер BZP / ocds-id — точне зіставлення з e-Zamówienia (без нечіткого дедупу)."""
        try:
            resp = self.http.get(n.url, ok=(200, 404))
        except HttpError:
            return n
        if resp.status_code != 200:
            return n
        text = clean(BeautifulSoup(resp.text, "html.parser").get_text(" "))
        for m in BZP_RX.findall(text):
            k = f"bzp:{m.replace('  ', ' ')}"
            if k not in n.keys:
                n.keys.append(k)
        for m in OCDS_RX.findall(text):
            k = f"ezam:{m}"
            if k not in n.keys:
                n.keys.append(k)
        n.body_text = text[:20_000]
        return n

    def doctor(self) -> list[tuple[str, bool, str]]:
        try:
            resp = self.http.get(f"{BASE}/all", params={"page": 1, "limit": 5, "query": "schron"})
            items = parse_listing(resp.text, "doctor")
            ok = bool(items) and all(n.source_id and n.title for n in items)
            return [("pz listing", ok, f"{len(items)} записів на 1-й сторінці")]
        except Exception as e:  # noqa: BLE001
            return [("pz listing", False, str(e))]
