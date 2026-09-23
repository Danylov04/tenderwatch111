"""Дрібні спільні утиліти: час, нормалізація тексту."""
from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")
KYIV = ZoneInfo("Europe/Kyiv")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(utcnow())


_FRAC = re.compile(r"(\.\d{6})\d+")


def parse_dt(value: str | None, default_tz=UTC) -> datetime | None:
    """Гнучкий парсер дат джерел -> aware datetime (UTC).

    Приймає: '2026-09-17T09:59:30.5103917Z', '2026-09-14+02:00', '2026-10-02', '22-09-2026 10:00:00 Europe/Warsaw'.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    # dd-mm-yyyy HH:MM:SS [Zone/Name]
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?(?:\s+([A-Za-z_]+/[A-Za-z_]+))?$", s)
    if m:
        d, mo, y, hh, mm, ss, tz = m.groups()
        zone = ZoneInfo(tz) if tz else default_tz
        return datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0), tzinfo=zone).astimezone(UTC)
    s = _FRAC.sub(r"\1", s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # 2026-09-14+02:00 (дата з офсетом, без часу)
    m = re.match(r"^(\d{4}-\d{2}-\d{2})([+-]\d{2}:\d{2})$", s)
    if m:
        s = f"{m.group(1)}T00:00:00{m.group(2)}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(UTC)


def to_iso(value: str | None, default_tz=UTC) -> str | None:
    dt = parse_dt(value, default_tz)
    return iso(dt) if dt else None


def day(value: str | None) -> str | None:
    """'YYYY-MM-DD' з ISO-рядка."""
    return value[:10] if value else None


def today() -> date:
    return utcnow().date()


_WS = re.compile(r"\s+")


def clean(text: str | None) -> str:
    return _WS.sub(" ", (text or "").replace("\xa0", " ")).strip()


def fold(text: str | None) -> str:
    """Нижній регістр, без діакритики (для порівняння польського тексту)."""
    t = unicodedata.normalize("NFKD", clean(text).lower().replace("ł", "l"))
    return "".join(c for c in t if not unicodedata.combining(c))


_STOP = {
    "gmina", "miasto", "powiat", "urzad", "miejski", "gminy", "miasta", "powiatu", "w", "im", "sp", "z", "o", "spolka",
    "oo", "zoo", "samodzielny", "publiczny", "zaklad", "opieki", "zdrowotnej",
}


def norm_buyer(name: str | None) -> str:
    toks = re.findall(r"[a-z0-9]+", fold(name))
    toks = [t for t in toks if t not in _STOP]
    return " ".join(sorted(set(toks)))


_REF = re.compile(r"^[A-Za-z0-9./\-_]{3,}\s+(?=[A-ZŁŚŻŹĆŃÓĄĘ„\"a-ząćęłńóśźż])")


def norm_title(title: str | None) -> str:
    """Назва без службового номера на початку, без розділових знаків."""
    t = clean(title)
    t = re.sub(r"\(ID\s*\d+\)\s*$", "", t).strip()
    # ведучий референс (ZP.271.22.2026 ...)
    m = re.match(r"^([A-Za-z]{1,6}[.\-/ ]?[\dA-Za-z./\-_]*\d[\dA-Za-z./\-_]*)\s+(.*)$", t)
    if m and re.search(r"\d", m.group(1)) and len(m.group(1)) <= 24:
        t = m.group(2)
    return " ".join(re.findall(r"[a-z0-9]+", fold(t)))


def trunc(text: str, n: int) -> str:
    text = clean(text)
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"
