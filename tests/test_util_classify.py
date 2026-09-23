from tenderwatch.classify import classify, needs_body_scan
from tenderwatch.models import OPEN, Notice
from tenderwatch.sources.ezam import parse_cpv, to_notice
from tenderwatch.util import norm_buyer, norm_title, parse_dt, to_iso

from .conftest import fixture_json, make_item


def test_parse_dt_variants():
    assert to_iso("2026-09-17T09:59:30.5103917Z") == "2026-09-17T09:59:30Z"
    assert to_iso("22-09-2026 10:00:00 Europe/Warsaw") == "2026-09-22T08:00:00Z"
    assert to_iso("2026-09-14+02:00") == "2026-09-13T22:00:00Z"
    assert to_iso("2026-10-02") == "2026-10-02T00:00:00Z"
    assert parse_dt("") is None and parse_dt("не дата") is None


def test_norm_title_strips_reference_and_id():
    a = norm_title("ZP.271.22.2026 Adaptacja piwnic na MDS (ID 1374414)")
    b = norm_title("Adaptacja piwnic na MDS")
    assert a == b


def test_norm_buyer_ignores_form_words():
    assert norm_buyer("Gmina Lublin") == norm_buyer("MIASTO LUBLIN")


def test_parse_cpv_keeps_labels_with_commas():
    codes, labels = parse_cpv(fixture_json("ezam_real_items.json")[1]["cpvCode"])
    assert codes[0] == "45216129" and "39150000" in codes
    assert labels["45111000"].startswith("Roboty w zakresie burzenia")


def test_ezam_real_items_normalize_and_classify(rules):
    lomza, oborniki = (to_notice(i, "t") for i in fixture_json("ezam_real_items.json"))
    assert lomza.keys[0].startswith("ezam:ocds-") and lomza.keys[1] == "bzp:2026/BZP 00442989"
    assert lomza.province == "podlaskie" and oborniki.province == "wielkopolskie"
    assert lomza.lifecycle == "awarded" and lomza.contractors[0]["name"].startswith("RUTKOWSKI")
    # Łomża: у назві нема слова «schron», але є сильний CPV 45216129 -> має бути relevant
    c = classify(lomza, rules)
    assert c.relevance == "relevant" and c.category == "shelter"
    assert any("45216129" in r for r in c.reasons)
    # Oborniki: MDS у назві + CPV
    assert classify(oborniki, rules).relevance == "relevant"


def n(title, **kw):
    return Notice(source="ezam", source_id="x", title=title, lifecycle=OPEN, **kw)


def test_noise_is_filtered(rules):
    for t in [
        "Budowa schroniska dla zwierząt w Testowie",
        "Ochrona obiektów i mienia Urzędu Gminy",
        "Zakup odzieży ochronnej dla pracowników",
        "Budowa wiaty przystankowej",
        "Ochrona środowiska - program edukacyjny",
        "Szkoła modułowa – oddział zespołu",
    ]:
        assert classify(n(t), rules).relevance == "noise", t


def test_positive_titles(rules):
    for t, rel in [
        ("Budowa ukrycia modułowego kategorii 1", "relevant"),
        ("Przygotowanie miejsca doraźnego schronienia w Prusicach", "relevant"),
        ("Budowa schronu w budynku szkoły", "relevant"),
        ("Rozwój infrastruktury podwójnego przeznaczenia, obiekty sportowe i obrona cywilna", "relevant"),
    ]:
        assert classify(n(t), rules).relevance == rel, t


def test_equipment_purchase_is_only_review(rules):
    c = classify(n("Zakup wyposażenia dla obrony cywilnej i ochrony ludności", kind="supplies"), rules)
    assert c.relevance in ("review", "noise")
    assert c.relevance != "relevant"


def test_body_text_can_promote_generic_title(rules):
    generic = n("Remont piwnic w Szkole Podstawowej nr 3", cpv=["45000000"])
    assert classify(generic, rules).relevance == "noise"
    assert needs_body_scan(generic, classify(generic, rules), rules)
    generic.description = "Przedmiotem zamówienia jest adaptacja piwnic na miejsce doraźnego schronienia."
    c = classify(generic, rules)
    assert c.relevance == "relevant" and any("(тіло)" in r for r in c.reasons)


def test_strong_cpv_beats_noise_word_partially(rules):
    x = n("Schronisko młodzieżowe - modernizacja", cpv=["45216129"])
    assert classify(x, rules).relevance in ("relevant", "review")


def test_make_item_helper_is_valid(rules):
    item = make_item("Budowa schronu")
    assert to_notice(item, "t").deadline is not None
