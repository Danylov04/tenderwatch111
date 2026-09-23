from tenderwatch import store
from tenderwatch.classify import classify
from tenderwatch.db import jload
from tenderwatch.sources.ezam import to_notice as ezam_notice
from tenderwatch.sources.pz import parse_listing
from tenderwatch.sources.ted import to_notice as ted_notice

from .conftest import fixture_json, fixture_text, iso_days, make_item


def ingest(conn, rules, n, persist_noise=True, **kw):
    return store.ingest(conn, n, classify(n, rules), persist_noise=persist_noise, **kw)


def events(conn, pk):
    return [r["kind"] for r in conn.execute("SELECT kind FROM events WHERE tender_pk=? ORDER BY pk", (pk,))]


def test_new_then_idempotent(conn, rules):
    n = ezam_notice(make_item("Budowa schronu w szkole"), "title:schron")
    r1 = ingest(conn, rules, n)
    assert r1.created and events(conn, r1.tender_pk) == ["new"]
    r2 = ingest(conn, rules, n)
    assert not r2.created and r2.tender_pk == r1.tender_pk and r2.events == []
    assert events(conn, r1.tender_pk) == ["new"]
    assert conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"] == 1


def test_hits_are_recorded_per_strategy(conn, rules):
    item = make_item("Budowa schronu w szkole", cpvCode="45216129-4 (Schrony)")
    pk = ingest(conn, rules, ezam_notice(item, "title:schron")).tender_pk
    ingest(conn, rules, ezam_notice(item, "cpv:45216129"))
    ingest(conn, rules, ezam_notice(item, "sweep"))
    strategies = {r["strategy"] for r in conn.execute("SELECT strategy FROM hits WHERE tender_pk=?", (pk,))}
    assert strategies == {"title:schron", "cpv:45216129", "sweep"}


def test_noise_from_sweep_is_not_stored_but_remembered(conn, rules):
    n = ezam_notice(make_item("Remont chodnika"), "sweep")
    r = ingest(conn, rules, n, persist_noise=False)
    assert r.tender_pk is None and not r.stored
    assert conn.execute("SELECT COUNT(*) c FROM tenders").fetchone()["c"] == 0
    assert store.is_seen(conn, "ezam", n.source_id)


def test_amendment_changes_deadline_and_emits_event(conn, rules):
    base = make_item("Budowa schronu w szkole", submittingOffersDate=iso_days(10), publicationDate=iso_days(-2))
    pk = ingest(conn, rules, ezam_notice(base, "title:schron")).tender_pk
    amended = dict(base, noticeNumber=base["noticeNumber"][:-2] + "02", objectId="08df0000-0000-0000-0000-aaaaaaaaaaaa",
                   submittingOffersDate=iso_days(17), publicationDate=iso_days(-1))
    r = ingest(conn, rules, ezam_notice(amended, "title:schron"))
    assert r.tender_pk == pk and "deadline_changed" in r.events
    t = conn.execute("SELECT * FROM tenders WHERE pk=?", (pk,)).fetchone()
    assert t["deadline"][:10] == amended["submittingOffersDate"][:10]
    ev = conn.execute("SELECT detail FROM events WHERE kind='deadline_changed'").fetchone()
    assert jload(ev["detail"])["old"][:10] == base["submittingOffersDate"][:10]
    assert conn.execute("SELECT COUNT(*) c FROM notices WHERE tender_pk=?", (pk,)).fetchone()["c"] == 2


def test_result_notice_closes_tender_even_if_title_is_noise_like(conn, rules):
    base = make_item("Budowa schronu w szkole", publicationDate=iso_days(-5))
    pk = ingest(conn, rules, ezam_notice(base, "title:schron")).tender_pk
    result = dict(base, noticeType="TenderResultNotice", objectId="08df0000-0000-0000-0000-bbbbbbbbbbbb",
                  publicationDate=iso_days(-1), submittingOffersDate=None, procedureResult="zawarcieUmowy",
                  contractors=[{"contractorName": "MOBIN Sp. z o.o.", "contractorCity": "Poznań", "contractorNationalId": "1"}])
    r = ingest(conn, rules, ezam_notice(result, "sweep"), persist_noise=False)   # sweep, але ключ уже відомий
    assert r.tender_pk == pk and "lifecycle_changed" in r.events
    t = conn.execute("SELECT * FROM tenders WHERE pk=?", (pk,)).fetchone()
    assert t["lifecycle"] == "awarded" and jload(t["contractors"])[0]["name"].startswith("MOBIN")


def test_cross_source_dedup_pz_then_ezam(conn, rules):
    """Węgliniec: той самий тендер з platformazakupowa та e-Zamówienia має злитися в один запис."""
    title = "Przygotowanie miejsca doraźnego schronienia (MDS) o podwójnym przeznaczeniu w szkołach w Węglińcu"
    pz = parse_listing(fixture_text("pz_listing.html"), "kw:schron")[0]
    pz.title, pz.buyer = title, "Gmina Węgliniec"
    pk = ingest(conn, rules, pz).tender_pk
    ez = ezam_notice(make_item(title, organizationName="GMINA WĘGLINIEC"), "title:MDS")
    r = ingest(conn, rules, ez)
    assert r.tender_pk == pk and r.linked_via == "fuzzy" and "source_added" in r.events
    assert conn.execute("SELECT COUNT(*) c FROM tenders").fetchone()["c"] == 1
    t = conn.execute("SELECT * FROM tenders WHERE pk=?", (pk,)).fetchone()
    assert set(jload(t["sources"])) == {"pz", "ezam"}
    assert set(jload(t["links"])) == {"pz", "ezam"}
    assert t["deadline_src"] == "ezam"     # точніше джерело виграє


def test_exact_key_link_beats_title_difference(conn, rules):
    pz = parse_listing(fixture_text("pz_listing.html"), "kw:schron")[0]
    pk = ingest(conn, rules, pz).tender_pk
    ez = ezam_notice(make_item("Zupełnie inna nazwa schronu w gminie X"), "title:schron")
    pz2 = parse_listing(fixture_text("pz_listing.html"), "kw:schron")[0]
    pz2.keys.append(ez.keys[0])     # pz.enrich знайшов ocds-id на сторінці
    ingest(conn, rules, pz2)
    r = ingest(conn, rules, ez)
    assert r.tender_pk == pk and r.linked_via == "key"


def test_ted_and_ezam_link_by_fuzzy_title(conn, rules):
    ted = [ted_notice(x, "cpv") for x in fixture_json("ted_response.json")["notices"]]
    pk = ingest(conn, rules, ted[0]).tender_pk
    r = ingest(conn, rules, ted[1])          # can-standard (результат) тієї ж процедури
    assert r.tender_pk == pk and "lifecycle_changed" in r.events
    assert conn.execute("SELECT lifecycle FROM tenders WHERE pk=?", (pk,)).fetchone()["lifecycle"] == "awarded"


def test_similar_titles_with_different_numbers_are_not_merged(conn, rules):
    """Лублін: дев'ять адаптацій шкіл — майже однакові назви, але різні процедури."""
    pks = {ingest(conn, rules, ezam_notice(make_item(f"Adaptacja pomieszczeń Szkoły Podstawowej nr {i} w Lublinie na schron", organizationName="Gmina Lublin"), "t")).tender_pk for i in (5, 7, 12)}
    assert len(pks) == 3


def test_parallel_identical_open_tenders_stay_separate(conn, rules):
    """Biesiekierz: два одночасні тендери з ОДНАКОВОЮ назвою — це дві процедури, не повторна публікація."""
    a = ezam_notice(make_item("Budowa ukrycia modułowego kategorii 1", organizationName="Gmina Biesiekierz"), "t")
    b = ezam_notice(make_item("Budowa ukrycia modułowego kategorii 1", organizationName="Gmina Biesiekierz"), "t")
    assert ingest(conn, rules, a).tender_pk != ingest(conn, rules, b).tender_pk


def test_pz_listing_is_not_glued_to_ambiguous_twins(conn, rules):
    title = "Budowa ukrycia modułowego kategorii 1 w gminie Biesiekierz"
    for _ in range(2):
        ingest(conn, rules, ezam_notice(make_item(title, organizationName="Gmina Biesiekierz"), "t"))
    pz = parse_listing(fixture_text("pz_listing.html"), "kw:schron")[0]
    pz.title, pz.buyer = title, "Gmina Biesiekierz"
    r = ingest(conn, rules, pz)
    assert r.created                    # неоднозначно -> окремий запис, а не хибне злиття


def test_award_notice_attaches_to_open_procedure_same_source(conn, rules):
    """TED: cn-standard (ще «open» у БД) і згодом can-standard тієї ж процедури не мають роздвоїтись."""
    ted = [ted_notice(x, "cpv") for x in fixture_json("ted_response.json")["notices"]]
    ted[0].lifecycle = "open"
    pk = ingest(conn, rules, ted[0]).tender_pk
    r = ingest(conn, rules, ted[1])
    assert r.tender_pk == pk and "lifecycle_changed" in r.events


def test_republished_after_cancellation(conn, rules):
    """Namysłów: двічі анульовано, третя публікація має бути тим самим записом зі спробою №2+."""
    t1 = make_item("Adaptacja pomieszczeń szkoły na MDS w Namysłowie", publicationDate=iso_days(-30),
                   submittingOffersDate=iso_days(-20), organizationName="Powiat Namysłowski")
    pk = ingest(conn, rules, ezam_notice(t1, "title:MDS")).tender_pk
    cancelled = dict(t1, noticeType="TenderResultNotice", objectId="08df0000-0000-0000-0000-cccccccccccc",
                     publicationDate=iso_days(-15), procedureResult="uniewaznienie", submittingOffersDate=None)
    ingest(conn, rules, ezam_notice(cancelled, "title:MDS"))
    assert conn.execute("SELECT lifecycle FROM tenders WHERE pk=?", (pk,)).fetchone()["lifecycle"] == "cancelled"
    t2 = make_item("Adaptacja pomieszczeń szkoły na MDS w Namysłowie", publicationDate=iso_days(-1),
                   submittingOffersDate=iso_days(9), organizationName="Powiat Namysłowski")
    r = ingest(conn, rules, ezam_notice(t2, "title:MDS"))
    assert r.tender_pk == pk and "republished" in r.events
    t = conn.execute("SELECT * FROM tenders WHERE pk=?", (pk,)).fetchone()
    assert t["lifecycle"] == "open" and t["attempts"] == 2


def test_merge_when_new_notice_links_two_tenders(conn, rules):
    a = ezam_notice(make_item("Budowa schronu A w gminie Alfa"), "title:schron")
    b = parse_listing(fixture_text("pz_listing.html"), "kw:schron")[0]
    pk_a = ingest(conn, rules, a).tender_pk
    pk_b = ingest(conn, rules, b).tender_pk
    assert pk_a != pk_b
    store.set_user_status(conn, pk_b, "watch")
    bridge = parse_listing(fixture_text("pz_listing.html"), "kw:schron")[0]
    bridge.keys.append(a.keys[0])
    r = ingest(conn, rules, bridge)
    assert r.tender_pk == min(pk_a, pk_b) and "merged" in r.events
    assert conn.execute("SELECT COUNT(*) c FROM tenders").fetchone()["c"] == 1
    t = conn.execute("SELECT * FROM tenders").fetchone()
    assert t["user_status"] == "watch"                       # ручний статус не губиться при злитті
    assert conn.execute("SELECT COUNT(DISTINCT tender_pk) c FROM tender_keys").fetchone()["c"] == 1


def test_relevance_only_goes_up_and_emits_event(conn, rules):
    item = make_item("Remont piwnic w szkole", cpvCode="45000000-7 (Roboty budowlane)")
    pk = ingest(conn, rules, ezam_notice(item, "title:ochronn")).tender_pk
    n2 = ezam_notice(item, "sweep")
    n2.description = "adaptacja piwnic na miejsce doraźnego schronienia"
    r = ingest(conn, rules, n2)
    assert r.tender_pk == pk and "relevance_changed" in r.events
    assert conn.execute("SELECT relevance FROM tenders WHERE pk=?", (pk,)).fetchone()["relevance"] == "relevant"
    n3 = ezam_notice(item, "sweep")          # без опису — не знижує
    ingest(conn, rules, n3)
    assert conn.execute("SELECT relevance FROM tenders WHERE pk=?", (pk,)).fetchone()["relevance"] == "relevant"


def test_set_user_status_validates(conn, rules):
    pk = ingest(conn, rules, ezam_notice(make_item("Budowa schronu"), "t")).tender_pk
    store.set_user_status(conn, pk, "applied", "wysłano ofertę")
    row = conn.execute("SELECT user_status, user_note FROM tenders WHERE pk=?", (pk,)).fetchone()
    assert (row["user_status"], row["user_note"]) == ("applied", "wysłano ofertę")
    import pytest
    with pytest.raises(ValueError):
        store.set_user_status(conn, pk, "bogus")
