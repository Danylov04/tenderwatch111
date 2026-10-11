import httpx

from tenderwatch import translate
from tenderwatch.db import connect
from tenderwatch.http import Http
from tenderwatch.web.staticsite import collect


def _db(tmp_path, rows):
    conn = connect(str(tmp_path / "t.sqlite3"))
    for i, (desc, rel, life) in enumerate(rows):
        conn.execute(
            "INSERT INTO tenders(title, relevance, lifecycle, description, first_seen, last_seen, last_changed) "
            "VALUES(?,?,?,?,?,?,?)",
            (f"T{i}", rel, life, desc, "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"),
        )
    return conn


class Fake:
    name = "fake"

    def __init__(self, fail_after=None):
        self.calls = 0
        self.fail_after = fail_after

    def translate(self, text):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("ліміт")
        return "UK:" + text


def test_chunks_respect_size_and_keep_text():
    text = "Zdanie pierwsze. " * 200
    parts = translate.chunks(text, 300)
    assert all(len(p) <= 300 for p in parts)
    assert " ".join(parts).split() == text.split()
    assert translate.chunks("x" * 1000, 300) and all(len(p) <= 300 for p in translate.chunks("x" * 1000, 300))


def test_translate_pending_caches_prioritises_and_skips_noise(tmp_path):
    conn = _db(tmp_path, [
        ("Opis przeglądu", "review", "open"),
        ("Opis schronu", "relevant", "open"),
        ("Szum", "noise", "open"),
        ("Opis schronu", "relevant", "closed"),   # дублікат тексту — перекладаємо один раз
    ])
    fake = Fake()
    rep = translate.translate_pending(conn, fake)
    assert rep["translated"] == 2 and fake.calls == 2 and rep["remaining"] == 0
    assert translate.lookup(conn, "Opis schronu") == "UK:Opis schronu"
    assert translate.lookup(conn, "Szum") == ""
    # повторний запуск нічого не перекладає
    assert translate.translate_pending(conn, Fake())["translated"] == 0


def test_budget_and_errors_stop_gracefully(tmp_path):
    conn = _db(tmp_path, [("a" * 100, "relevant", "open"), ("b" * 100, "relevant", "open"), ("c" * 100, "relevant", "open")])
    rep = translate.translate_pending(conn, Fake(), budget=150)
    assert rep["translated"] == 1 and rep["remaining"] == 2
    rep = translate.translate_pending(conn, Fake(fail_after=1))
    assert rep["translated"] == 1 and rep["error"] == "ліміт" and rep["remaining"] == 1


def test_google_translator_parses_response():
    def handler(req):
        assert req.url.params["sl"] == "pl" and req.url.params["tl"] == "uk"
        return httpx.Response(200, json=[[["Будівництво укриття. ", "Budowa schronu. ", None], ["Кінець", "Koniec", None]], None, "pl"])

    http = Http(interval=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert translate.GoogleTranslator(http).translate("Budowa schronu. Koniec") == "Будівництво укриття. Кінець"


def test_deepl_selected_by_key_and_parses():
    def handler(req):
        assert req.url.host == "api-free.deepl.com" and req.headers["Authorization"] == "DeepL-Auth-Key k:fx"
        return httpx.Response(200, json={"translations": [{"text": "Укриття"}]})

    http = Http(interval=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    tr = translate.make_translator(http, deepl_key="k:fx")
    assert tr.name == "deepl" and tr.translate("Schron") == "Укриття"
    assert translate.make_translator(http, deepl_key="").name == "google"


def test_dashboard_data_carries_ukrainian_description(tmp_path):
    conn = _db(tmp_path, [("Opis schronu", "relevant", "open"), ("Bez tłumaczenia", "relevant", "open")])
    translate.translate_pending(conn, Fake(), budget=12)
    items = {i["description"]: i for i in collect(conn, None)["items"]}
    assert items["Opis schronu"]["description_uk"] == "UK:Opis schronu"
    assert items["Bez tłumaczenia"]["description_uk"] == ""
