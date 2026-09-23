"""Зіставлення записів між джерелами й повторними публікаціями.

1) точні ключі (ocds-id, номер BZP, TED publication-number, id на платформі) — основний шлях;
2) нечітке: той самий замовник (перетин токенів) + майже ідентична назва.
"""
from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher

from .db import jload
from .models import Notice
from .util import norm_buyer, norm_title

CROSS_SOURCE_THRESHOLD = 0.93
TITLE_THRESHOLD_NO_BUYER = 0.97


def find_by_keys(conn: sqlite3.Connection, keys: list[str]) -> list[int]:
    if not keys:
        return []
    q = ",".join("?" * len(keys))
    rows = conn.execute(f"SELECT DISTINCT tender_pk FROM tender_keys WHERE key IN ({q}) ORDER BY tender_pk", keys).fetchall()
    return [r["tender_pk"] for r in rows]


def buyer_similarity(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if abs(len(a) - len(b)) > 0.35 * max(len(a), len(b)):
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _numbers(norm: str) -> frozenset[str]:
    return frozenset(re.findall(r"\d+", norm))


def find_fuzzy(conn: sqlite3.Connection, n: Notice) -> int | None:
    """Нечітке зіставлення. Свідомо обережне: хибне злиття двох різних тендерів гірше за пропущений дубль.

    Правила:
    * числа в назвах мусять збігатися (nr 5 ≠ nr 7; «Szkoła nr 3 w Lublinie» — різні процедури);
    * інше ДЖЕРЕЛО (pz ↔ ezam ↔ ted): схожість ≥ 0.93 та збіг замовника;
    * те саме джерело = потенційна ПОВТОРНА ПУБЛІКАЦІЯ: лише ідентична назва й лише якщо попередня спроба вже завершена
      (два одночасні відкриті тендери з однаковою назвою — різні процедури, напр. два «ukrycie modułowe» в одній гміні);
    * якщо кандидатів кілька — беремо того, чий дедлайн збігається за датою; інакше НЕ зливаємо (неоднозначно).
    """
    tn = norm_title(n.title)
    if len(tn) < 12:
        return None
    bn = norm_buyer(n.buyer)
    nums = _numbers(tn)
    cands: list[tuple[float, sqlite3.Row]] = []
    for r in conn.execute("SELECT pk, title_norm, buyer_norm, sources, lifecycle, deadline FROM tenders"):
        if _numbers(r["title_norm"]) != nums:
            continue
        ts = title_similarity(tn, r["title_norm"])
        same_source = n.source in (jload(r["sources"], []) or [])
        if same_source:
            incoming_active = n.lifecycle in ("open", "planned", "unknown")
            if ts < 1.0 or (incoming_active and r["lifecycle"] in ("open", "planned", "unknown")):
                continue
        elif ts < CROSS_SOURCE_THRESHOLD:
            continue
        if bn and r["buyer_norm"]:
            if buyer_similarity(bn, r["buyer_norm"]) < 0.5:
                continue
        elif ts < TITLE_THRESHOLD_NO_BUYER:
            continue
        cands.append((ts, r))
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0][1]["pk"]
    by_deadline = [r for _, r in cands if n.deadline and r["deadline"] and r["deadline"][:10] == n.deadline[:10]]
    if len(by_deadline) == 1:
        return by_deadline[0]["pk"]
    return None
