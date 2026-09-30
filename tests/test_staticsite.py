"""Статичний дашборд (web/staticsite.py): дані реально зібрані з БД потрапляють у згенерований HTML."""
from __future__ import annotations

import json

from tenderwatch.daemon import run_sources
from tenderwatch.web import staticsite

from .conftest import FakeEzam, make_http, make_item


def _seed(conn, settings, rules):
    rules.raw["search"]["title_keywords"] = ["schron"]
    rules.raw["search"]["cpv_codes"] = ["45216129"]
    rules.raw["search"]["pz_keywords"] = []
    items = [make_item("Budowa schronu w Szkole Podstawowej", cpvCode="45216129-4 (Schrony)")]
    http = make_http(ezam=FakeEzam(items, {}))
    reports = run_sources(conn, settings, rules, "search", 3, http, sources=["ezam"])
    assert reports[0].ok and reports[0].new_tenders == 1


def _embedded_data(html: str) -> dict:
    start = html.index("const DATA = ") + len("const DATA = ")
    end = html.index(";\n", start)
    return json.loads(html[start:end])


def test_build_embeds_tender_data_and_replaces_placeholder(tmp_path, conn, settings, rules):
    _seed(conn, settings, rules)
    out = staticsite.build(conn, rules, str(tmp_path / "docs" / "index.html"))
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert staticsite.PLACEHOLDER not in html

    data = _embedded_data(html)
    assert data["total"] == 1
    item = data["items"][0]
    assert item["title"] == "Budowa schronu w Szkole Podstawowej"
    assert item["relevance"] in ("relevant", "review")
    assert "notices" in item and "events" in item and "hits" in item and "keys" in item
    assert "user_note" not in item                      # приватне поле не потрапляє в публічну сторінку

    assert data["health"]["sources"]
    assert "province" in data["facets"] and "category" in data["facets"]
    assert "recall" in data                             # rules передано — звіт повноти має бути присутній


def test_build_without_rules_skips_recall(tmp_path, conn, settings, rules):
    _seed(conn, settings, rules)
    out = staticsite.build(conn, None, str(tmp_path / "docs" / "index.html"))
    data = _embedded_data(out.read_text(encoding="utf-8"))
    assert "recall" not in data


def test_collect_respects_relevance_filter(tmp_path, conn, settings, rules):
    _seed(conn, settings, rules)
    data = staticsite.collect(conn, rules, relevance=("noise",))
    assert data["total"] == 0
