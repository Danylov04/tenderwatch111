from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tenderwatch.sources import EzamSource, PzSource, TedSource, Window
from tenderwatch.sources.base import FilterIgnored, SourceError
from tenderwatch.sources.ezam_body import parse_amount, parse_body
from tenderwatch.sources.pz import parse_listing

from .conftest import FakeEzam, fixture_json, fixture_text, iso_days, make_http, make_item

WINDOW = Window(datetime.now(UTC) - timedelta(days=3), datetime.now(UTC))


def small_rules(rules, keywords=("schron",), cpvs=()):
    rules.raw["search"]["title_keywords"] = list(keywords)
    rules.raw["search"]["cpv_codes"] = list(cpvs)
    return rules


def test_ezam_title_search_paginates_and_stops_at_window(rules):
    items = [make_item(f"Budowa schronu nr {i}", publicationDate=iso_days(-0.01 * i)) for i in range(1, 26)]
    old = make_item("Stary schron", publicationDate=iso_days(-30))
    fake = FakeEzam(items + [old])
    src = EzamSource(make_http(ezam=fake), small_rules(rules))
    got = list(src.search(WINDOW))
    assert len(got) == 25                      # 3 сторінки по 10, стара публікація відсічена вікном
    assert all(n.strategy == "title:schron" for n in got)
    assert src.stats.pages == 3


def test_ezam_cpv_search_uses_cpv_filter(rules):
    a = make_item("Adaptacja piwnic", cpvCode="45216129-4 (Schrony),45000000-7 (Roboty budowlane)")
    b = make_item("Remont dachu", cpvCode="45261000-4 (Roboty dachowe)")
    src = EzamSource(make_http(ezam=FakeEzam([a, b])), small_rules(rules, keywords=(), cpvs=("45216129",)))
    got = list(src.search(WINDOW))
    assert [n.title for n in got] == ["Adaptacja piwnic"]
    assert got[0].strategy == "cpv:45216129"


def test_ezam_silently_ignored_filter_is_detected(rules):
    """Регресія на реальну поведінку: сервер ігнорує невідомий/неробочий параметр і віддає все підряд."""
    items = [make_item(f"Zupełnie inny przetarg {i}") for i in range(10)]
    src = EzamSource(make_http(ezam=FakeEzam(items, ignore_filters=True)), small_rules(rules))
    with pytest.raises(FilterIgnored):
        list(src.search(WINDOW))


def test_ezam_unexpected_schema_raises(rules):
    class Weird(FakeEzam):
        def __call__(self, request):
            import httpx
            return httpx.Response(200, json={"items": []})

    src = EzamSource(make_http(ezam=Weird()), small_rules(rules))
    with pytest.raises(SourceError):
        list(src.search(WINDOW))


def test_ezam_sweep_covers_window_and_all_types(rules):
    a = make_item("Cokolwiek 1")
    b = make_item("Cokolwiek 2", noticeType="TenderResultNotice", submittingOffersDate=None, publicationDate=iso_days(-1))
    c = make_item("Za stare", publicationDate=iso_days(-20))
    src = EzamSource(make_http(ezam=FakeEzam([a, b, c])), rules)
    got = list(src.sweep(WINDOW))
    assert {n.title for n in got} == {"Cokolwiek 1", "Cokolwiek 2"}
    assert all(n.strategy == "sweep" for n in got)


def test_ezam_unknown_notice_types_are_reported(rules):
    x = make_item("Schron zmiana", noticeType="ContractChangeNotice")
    src = EzamSource(make_http(ezam=FakeEzam([x])), small_rules(rules))
    got = list(src.search(WINDOW))
    assert got[0].lifecycle == "open"
    assert "ContractChangeNotice" in src.stats.unknown_types


def test_ezam_enrich_parses_body(rules):
    item = make_item("Przygotowanie miejsca doraźnego schronienia w Prusicach")
    fake = FakeEzam([item], bodies={item["objectId"]: fixture_text("ezam_body_prusice.html")})
    src = EzamSource(make_http(ezam=fake), rules)
    from tenderwatch.sources.ezam import to_notice
    n = src.enrich(to_notice(item, "t"))
    assert "doraźnego schronienia" in n.description and "84,71 m2" in n.description
    assert n.reference == "RZK-II.271.7.2026"
    assert n.contact_email == "gminazlotoryja@zlotoryja.con.pl"
    assert "45453000" in n.cpv and "45000000" in n.cpv
    assert n.lots == 1
    assert "noticeId=" + item["objectId"] in fake.calls[-1]


def test_ezam_body_404_is_not_fatal(rules):
    item = make_item("X schron")
    src = EzamSource(make_http(ezam=FakeEzam([item])), rules)
    from tenderwatch.sources.ezam import to_notice
    n = to_notice(item, "t")
    assert src.enrich(n) is n and not n.body_text


def test_parse_body_fields():
    b = parse_body(fixture_text("ezam_body_prusice.html"))
    assert b["buyer_name"] == "Gmina Złotoryja"
    assert b["plan_number"] == "2026/BZP 00068672/05/P"
    assert b["eu_funded"] is False
    assert b["cpv"][0] == "45000000"


def test_parse_amount():
    assert parse_amount("4 700 000,00") == 4_700_000.0
    assert parse_amount("1.234.567,89") == 1_234_567.89
    assert parse_amount("") is None


def test_ted_search_parses_and_paginates(rules):
    import httpx

    calls = []

    def ted(request):
        import json
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json=fixture_json("ted_response.json"))

    src = TedSource(make_http(ted=ted), rules)
    got = list(src.search(WINDOW))
    assert len(got) == 2
    cn, can = got
    assert cn.title.startswith("Budowa miejsca schronienia i miejsca ewakuacji szpitala")   # без «Polska – <CPV> –»
    assert cn.buyer.startswith("Samodzielny Publiczny Zakład Opieki Zdrowotnej")
    assert cn.keys == ["ted:557381-2026"] and cn.lifecycle == "closed" and can.lifecycle == "awarded"
    assert cn.cpv.count("45216129") == 1
    query = calls[0]["query"]
    assert "buyer-country=POL" in query
    assert query.count("publication-date>=") == 1
    # Регресія 2026-09-24: `classification-cpv=A OR classification-cpv=B OR ...` у дужках на практиці
    # валив check_filter (TED, схоже, неправильно парсить дужки/OR і фільтр фактично ігнорується).
    # Має бути `classification-cpv IN (A B C ...)`, а не ланцюжок OR.
    assert "classification-cpv IN (45216129" in query
    assert " OR " not in query


def test_ted_rejects_unfiltered_garbage(rules):
    import httpx

    data = fixture_json("ted_response.json")
    for nn in data["notices"]:
        nn["classification-cpv"] = ["45000000"]
    src = TedSource(make_http(ted=lambda r: httpx.Response(200, json=data)), rules)
    with pytest.raises(FilterIgnored):
        list(src.search(WINDOW))


def test_pz_listing_parser():
    got = parse_listing(fixture_text("pz_listing.html"), "kw:schron")
    assert [n.source_id for n in got] == ["1374414", "1375063"]
    a = got[0]
    assert a.title.endswith("na miejsce doraźnego schronienia") and "(ID" not in a.title
    assert a.buyer == "Gmina Wołomin"
    assert a.deadline == "2026-09-22T08:00:00Z"
    assert a.url == "https://platformazakupowa.pl/transakcja/1374414"


def test_pz_listing_layout_change_is_detected():
    with pytest.raises(SourceError):
        parse_listing("<html><body><p>Nowa strona</p></body></html>", "x")


def test_pz_search_stops_when_no_new_ids(rules):
    import httpx

    html = fixture_text("pz_listing.html")
    rules = small_rules(rules)
    rules.raw["search"]["pz_keywords"] = ["schron"]
    src = PzSource(make_http(pz=lambda r: httpx.Response(200, text=html)), rules)
    got = list(src.search(WINDOW))
    assert len(got) == 2 and src.stats.pages == 2


def test_pz_enrich_extracts_cross_links(rules):
    import httpx

    page = "<html><body>Postępowanie prowadzone także na BZP: 2026/BZP 00420579 oraz ocds-148610-094b52b1-1851-4d23-9f2f-78ac36f0c389</body></html>"
    src = PzSource(make_http(pz=lambda r: httpx.Response(200, text=page)), rules)
    n = parse_listing(fixture_text("pz_listing.html"), "x")[0]
    src.enrich(n)
    assert "bzp:2026/BZP 00420579" in n.keys
    assert "ezam:ocds-148610-094b52b1-1851-4d23-9f2f-78ac36f0c389" in n.keys


def test_ezam_sweep_goes_day_by_day(rules):
    from datetime import UTC, datetime, timedelta

    items = [make_item(f"Cokolwiek {i}", publicationDate=iso_days(-i + 0.01)) for i in range(0, 4)]
    fake = FakeEzam(items)
    src = EzamSource(make_http(ezam=fake), rules)
    got = list(src.sweep(Window(datetime.now(UTC) - timedelta(days=3), datetime.now(UTC))))
    assert len(got) == 4
    day_queries = [c for c in fake.calls if "PublicationDateFrom" in c]
    assert len(day_queries) >= 4 and all(_from_eq_to(c) for c in day_queries)


def _from_eq_to(url: str) -> bool:
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(url).query)
    return q["PublicationDateFrom"] == q["PublicationDateTo"]


class _FlakyDayEzam(FakeEzam):
    """Один конкретний день (PublicationDateFrom == bad_day) завжди повертає 500."""

    def __init__(self, items, bad_days: set[str]):
        super().__init__(items)
        self.bad_days = bad_days

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        if q.get("PublicationDateFrom") in self.bad_days:
            self.calls.append(str(request.url))
            return httpx.Response(500, text="boom")
        return super().__call__(request)


def test_ezam_sweep_skips_single_bad_day_but_keeps_the_rest(rules):
    """Регресія: до фіксу один збійний день у sweep обривав ВЕСЬ діапазон (втрачались усі вже оброблені дні)."""
    items = [make_item(f"Cokolwiek {i}", publicationDate=iso_days(-i + 0.01)) for i in range(0, 5)]
    bad_day = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%d")
    fake = _FlakyDayEzam(items, bad_days={bad_day})
    src = EzamSource(make_http(ezam=fake), rules)

    got = list(src.sweep(Window(datetime.now(UTC) - timedelta(days=4), datetime.now(UTC))))

    got_titles = {n.title for n in got}
    assert "Cokolwiek 2" not in got_titles           # збійний день пропущено...
    assert got_titles == {"Cokolwiek 0", "Cokolwiek 1", "Cokolwiek 3", "Cokolwiek 4"}  # ...а решта днів оброблена
    assert any("пропущено" in w and bad_day in w for w in src.stats.warnings)


def test_ezam_sweep_gives_up_after_too_many_consecutive_bad_days(rules):
    """3 дні поспіль впали -> це вже не глюк, а системна проблема: sweep має підняти помилку, а не мовчати."""
    items = [make_item(f"Cokolwiek {i}", publicationDate=iso_days(-i + 0.01)) for i in range(0, 5)]
    bad_days = {(datetime.now(UTC) - timedelta(days=d)).strftime("%Y-%m-%d") for d in (1, 2, 3)}
    fake = _FlakyDayEzam(items, bad_days=bad_days)
    src = EzamSource(make_http(ezam=fake), rules)

    with pytest.raises(SourceError):
        list(src.sweep(Window(datetime.now(UTC) - timedelta(days=4), datetime.now(UTC))))
