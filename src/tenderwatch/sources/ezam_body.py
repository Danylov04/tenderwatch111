"""Розбір HTML-тіла ogłoszenia BZP (Board/GetNoticeHtmlBodyById) у структуровані поля.

Тіло — це нумерована форма («4.2.2.) Krótki opis...», «1.5.9.) Adres poczty...»), тому надійніше за номерами полів,
ніж за версткою. Усі регулярні вирази терплять відсутні пробіли між тегами (get_text з роздільником).
"""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from ..util import clean

_END = r"(?=\s\d\.\d+(?:\.\d+)*\.?\)|\sSEKCJA\b|$)"


def to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["style", "script"]):
        tag.decompose()
    return clean(soup.get_text(" "))


def _field(text: str, label_rx: str, cap: str = r"(.*?)") -> str | None:
    m = re.search(label_rx + r"\s*" + cap + _END, text, re.S)
    return clean(m.group(1)) if m else None


def parse_amount(raw: str | None) -> float | None:
    if not raw:
        return None
    s = re.sub(r"[^\d,.]", "", raw)
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_body(html: str) -> dict[str, Any]:
    text = to_text(html)
    out: dict[str, Any] = {"text": text[:60_000]}

    out["description"] = _field(text, r"4\.2\.2\.\)\s*Krótki opis przedmiotu zamówienia") or ""
    out["reference"] = _field(text, r"4\.1\.2\.\)\s*Numer referencyjny:", r"(\S+)") or ""
    email = re.search(r"1\.5\.9\.\)\s*Adres poczty elektronicznej:\s*(\S+@\S+)", text)
    out["contact_email"] = email.group(1).strip().rstrip(".,;") if email else ""
    out["buyer_name"] = _field(text, r"1\.2\.\)\s*Nazwa zamawiającego:") or ""

    main = re.search(r"4\.2\.6\.\)\s*Główny kod CPV:\s*(\d{8})", text)
    add = re.search(r"4\.2\.7\.\)\s*Dodatkowy kod CPV:(.*?)(?=\s4\.2\.8\.\)|\s4\.3\.|\sSEKCJA|$)", text, re.S)
    cpv = [main.group(1)] if main else []
    if add:
        cpv += re.findall(r"\b(\d{8})-\d", add.group(1)) or re.findall(r"\b(\d{8})\b", add.group(1))
    out["cpv"] = list(dict.fromkeys(cpv))

    val = re.search(
        r"(?:Szacunkowa wartość(?: zamówienia)?|Wartość (?:zamówienia|szacunkowa|całkowita)[^:]{0,60}):\s*([\d\s.,]+)\s*(PLN|EUR|zł)?",
        text,
    )
    if val:
        out["value_amount"] = parse_amount(val.group(1))
        cur = (val.group(2) or "").replace("zł", "PLN")
        out["value_currency"] = cur or "PLN"

    dl = re.search(r"Termin składania ofert:\s*(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)", text)
    out["deadline_text"] = dl.group(1) if dl else ""

    parts = re.search(r"4\.1\.4\.\)[^:]*częściach[^:]*:\s*(Tak|Nie)", text)
    if parts:
        if parts.group(1) == "Nie":
            out["lots"] = 1
        else:
            n = len(set(re.findall(r"(?:Część|część)\s+nr\s+(\d+)|Numer części:?\s*(\d+)", text)))
            out["lots"] = n or None
    eu = re.search(r"współfinansowanego ze środków Unii Europejskiej:\s*(Tak|Nie)", text)
    out["eu_funded"] = (eu.group(1) == "Tak") if eu else None
    plan = re.search(r"Numer planu postępowań w BZP:\s*(\d{4}/BZP\s?\d+(?:/\d+)*(?:/P)?)", text)
    out["plan_number"] = plan.group(1) if plan else ""
    return out
