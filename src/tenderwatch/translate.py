"""Переклад опису тендера (предмета замовлення) з польської на українську для дашборду.

Переклади кешуються в таблиці `translations` за хешем вихідного тексту: кожен опис перекладається
один раз, а якщо джерело змінить опис — новий текст отримає новий хеш і перекладеться заново.
Кеш живе в тій самій SQLite-базі, тому в GitHub Actions переклад робиться в workflow `search`
(він зберігає базу між запусками), а `dashboard` лише читає готові переклади.

Провайдери:
- DeepL (якщо задано DEEPL_API_KEY) — найкраща якість; безкоштовний план DeepL API Free: 500 тис. символів/міс.
- Google Translate (публічний endpoint без ключа) — за замовчуванням, нічого налаштовувати не треба.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3

from .http import Http, HttpError
from .util import now_iso

log = logging.getLogger("tenderwatch.translate")

TARGET = "uk"
SOURCE = "pl"
CHUNK = 1500          # символів на один запит (Google GET має обмеження на довжину URL)
DEFAULT_BUDGET = 40000  # символів на один запуск (щогодинний search), щоб не впертися в ліміти провайдера

SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
  src_hash TEXT PRIMARY KEY,
  lang     TEXT NOT NULL,
  provider TEXT NOT NULL,
  text     TEXT NOT NULL,
  at       TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def src_hash(text: str, lang: str = TARGET) -> str:
    return hashlib.sha1(f"{lang}\n{text.strip()}".encode("utf-8")).hexdigest()


def lookup(conn: sqlite3.Connection, text: str, lang: str = TARGET) -> str:
    """Готовий переклад або '' (без мережі)."""
    if not text or not text.strip():
        return ""
    ensure_schema(conn)
    row = conn.execute("SELECT text FROM translations WHERE src_hash=?", (src_hash(text, lang),)).fetchone()
    return row["text"] if row else ""


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Ріже текст на шматки ≤ size по межах речень (а якщо речення задовге — по словах)."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    parts = re.split(r"(?<=[.;:!?])\s+", text)
    out: list[str] = []
    cur = ""
    for part in parts:
        while len(part) > size:  # дуже довге «речення» без розділових знаків
            cut = part.rfind(" ", 0, size)
            cut = cut if cut > 0 else size
            if cur:
                out.append(cur)
                cur = ""
            out.append(part[:cut])
            part = part[cut:].lstrip()
        if cur and len(cur) + 1 + len(part) > size:
            out.append(cur)
            cur = part
        else:
            cur = f"{cur} {part}" if cur else part
    if cur:
        out.append(cur)
    return out


class GoogleTranslator:
    name = "google"
    URL = "https://translate.googleapis.com/translate_a/single"

    def __init__(self, http: Http):
        self.http = http

    def translate(self, text: str) -> str:
        res = []
        for piece in chunks(text):
            data = self.http.get_json(self.URL, params={"client": "gtx", "sl": SOURCE, "tl": TARGET, "dt": "t", "q": piece})
            try:
                res.append("".join(seg[0] for seg in data[0] if seg and seg[0]))
            except (TypeError, IndexError) as e:
                raise HttpError(f"неочікувана відповідь перекладача: {e}") from e
        return " ".join(x.strip() for x in res if x.strip())


class DeepLTranslator:
    name = "deepl"

    def __init__(self, http: Http, key: str):
        self.http = http
        self.key = key
        host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
        self.url = f"https://{host}/v2/translate"

    def translate(self, text: str) -> str:
        res = []
        for piece in chunks(text, 4000):
            data = self.http.post_json(
                self.url,
                {"text": [piece], "source_lang": SOURCE.upper(), "target_lang": TARGET.upper()},
                headers={"Authorization": f"DeepL-Auth-Key {self.key}"},
            )
            try:
                res.append(data["translations"][0]["text"])
            except (KeyError, IndexError, TypeError) as e:
                raise HttpError(f"неочікувана відповідь DeepL: {e}") from e
        return " ".join(x.strip() for x in res if x.strip())


def make_translator(http: Http, deepl_key: str | None = None):
    key = deepl_key if deepl_key is not None else os.environ.get("DEEPL_API_KEY", "")
    return DeepLTranslator(http, key) if key else GoogleTranslator(http)


def pending(conn: sqlite3.Connection, relevance: tuple[str, ...] = ("relevant", "review")) -> list[str]:
    """Описи без перекладу, в порядку важливості: релевантні → на перевірку, відкриті → решта, ближчий дедлайн."""
    ensure_schema(conn)
    q = ",".join("?" * len(relevance))
    rows = conn.execute(
        f"""SELECT description FROM tenders
            WHERE relevance IN ({q}) AND description != ''
            ORDER BY CASE relevance WHEN 'relevant' THEN 0 ELSE 1 END,
                     CASE lifecycle WHEN 'open' THEN 0 WHEN 'planned' THEN 1 ELSE 2 END,
                     (deadline IS NULL), deadline DESC""",
        list(relevance),
    ).fetchall()
    seen: set[str] = set()
    out = []
    for r in rows:
        text = r["description"].strip()
        h = src_hash(text)
        if not text or h in seen:
            continue
        seen.add(h)
        if not conn.execute("SELECT 1 FROM translations WHERE src_hash=?", (h,)).fetchone():
            out.append(text)
    return out


def translate_pending(conn: sqlite3.Connection, translator, budget: int = DEFAULT_BUDGET,
                      relevance: tuple[str, ...] = ("relevant", "review")) -> dict:
    """Перекладає ще не перекладені описи в межах бюджету символів. Помилка перекладача не валить запуск."""
    todo = pending(conn, relevance)
    done = chars = 0
    error = ""
    for text in todo:
        if chars and chars + len(text) > budget:
            break
        try:
            uk = translator.translate(text)
        except Exception as e:  # noqa: BLE001 — мережа/ліміт: зупиняємось, решту доробить наступний запуск
            error = str(e)
            log.warning("переклад зупинено: %s", e)
            break
        if not uk:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO translations(src_hash, lang, provider, text, at) VALUES(?,?,?,?,?)",
            (src_hash(text), TARGET, translator.name, uk, now_iso()),
        )
        done += 1
        chars += len(text)
    return {"translated": done, "chars": chars, "remaining": len(todo) - done, "provider": translator.name, "error": error}
