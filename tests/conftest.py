from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tenderwatch.config import Rules, Settings, load_rules
from tenderwatch.db import connect
from tenderwatch.http import Http
from tenderwatch.util import fold

FIX = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture_text(name))


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.db_path = str(tmp_path / "t.sqlite3")
    s.telegram_token = "T"
    s.telegram_chat_id = "C"
    s.request_interval = 0
    s.public_url = "https://tw.example"
    return s


@pytest.fixture
def rules():
    # deepcopy: load_rules кешується, а тести змінюють ключові слова
    return Rules(copy.deepcopy(load_rules("").raw))


@pytest.fixture
def conn(settings):
    c = connect(settings.db_path)
    yield c
    c.close()


def iso_days(days: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


_counter = [0]


def make_item(title: str, **kw) -> dict:
    """Запис Board/Search за зразком реальної відповіді."""
    base = copy.deepcopy(fixture_json("ezam_real_items.json")[1])
    _counter[0] += 1
    n = _counter[0]
    base.update(
        noticeType="ContractNotice",
        noticeNumber=f"2026/BZP {n:08d}/01",
        bzpNumber=f"2026/BZP {n:08d}",
        orderObject=title,
        cpvCode="45000000-7 (Roboty budowlane)",
        submittingOffersDate=iso_days(10),
        procedureResult=None,
        contractors=[],
        tenderId=f"ocds-148610-{n:08d}-0000-0000-0000-000000000000",
        objectId=f"08df0000-0000-0000-0000-{n:012d}",
        publicationDate=iso_days(-0.1),
        organizationName="Gmina Testowa",
        organizationCity="Testowo",
        organizationProvince="PL14",
        organizationNationalId="1234567890",
    )
    base.update(kw)
    return base


class FakeEzam:
    """Імітація Board/Search: фільтри OrderObject/CpvCode/NoticeType/PublicationDateFrom+To, пагінація по 10.

    ignore_filters=True відтворює реальну поведінку «невідомий параметр мовчки ігнорується».
    """

    def __init__(self, items=None, bodies=None, ignore_filters=False, fail_status=None):
        self.items = list(items or [])
        self.bodies = dict(bodies or {})
        self.ignore_filters = ignore_filters
        self.fail_status = fail_status
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if self.fail_status:
            return httpx.Response(self.fail_status, text="boom")
        path = request.url.path
        q = dict(request.url.params)
        if path.endswith("/Board/Search"):
            items = list(self.items)
            if not self.ignore_filters:
                if "OrderObject" in q:
                    items = [i for i in items if fold(q["OrderObject"]) in fold(i["orderObject"])]
                if "CpvCode" in q:
                    items = [i for i in items if q["CpvCode"] in (i.get("cpvCode") or "")]
                if "NoticeType" in q:
                    items = [i for i in items if i["noticeType"] == q["NoticeType"]]
                if "PublicationDateFrom" in q:
                    items = [i for i in items if i["publicationDate"][:10] >= q["PublicationDateFrom"]]
                if "PublicationDateTo" in q:
                    items = [i for i in items if i["publicationDate"][:10] <= q["PublicationDateTo"]]
            items.sort(key=lambda i: i["publicationDate"], reverse=(q.get("SortingDirection", "DESC") == "DESC"))
            page = int(q.get("PageNumber", 1))
            size = min(int(q.get("PageSize", 10)), 10)
            return httpx.Response(200, json=items[(page - 1) * size: page * size])
        if path.endswith("/GetNoticeHtmlBodyById"):
            body = self.bodies.get(q.get("noticeId"))
            if body is None:
                return httpx.Response(404, json={"error": "NotFound"})
            return httpx.Response(200, text=body)
        return httpx.Response(404)


def make_http(ezam=None, ted=None, pz=None, bk=None) -> Http:
    def route(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "ezamowienia.gov.pl" and ezam:
            return ezam(request)
        if host == "api.ted.europa.eu" and ted:
            return ted(request)
        if host == "platformazakupowa.pl" and pz:
            return pz(request)
        if host == "bazakonkurencyjnosci.gov.pl" and bk:
            return bk(request)
        return httpx.Response(599, text=f"no handler for {host}")

    return Http(interval=0, transport=httpx.MockTransport(route), sleep=lambda s: None, max_tries=2)
