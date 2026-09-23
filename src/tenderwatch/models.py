"""Нормалізований запис із будь-якого джерела."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Життєвий цикл (спільний для джерел)
OPEN = "open"
PLANNED = "planned"
AWARDED = "awarded"
CLOSED = "closed"
CANCELLED = "cancelled"
UNKNOWN = "unknown"


@dataclass
class Notice:
    source: str                      # 'ezam' | 'ted' | 'pz' | 'bk'
    source_id: str                   # унікальний у межах джерела (для версійних джерел — id конкретного оголошення)
    title: str
    strategy: str = ""               # яка стратегія знайшла: 'title:schron', 'cpv:45216129', 'sweep', 'kw:schron' ...
    keys: list[str] = field(default_factory=list)   # ідентифікатори процедури: 'ezam:ocds-..', 'bzp:2026/BZP 00446536', 'ted:557381-2026', 'pz:1374414'
    notice_type: str = ""
    lifecycle: str = UNKNOWN
    published_at: str | None = None  # ISO UTC
    deadline: str | None = None      # ISO UTC (або дата)
    url: str = ""
    buyer: str = ""
    buyer_nip: str = ""
    province: str = ""
    city: str = ""
    kind: str = ""                   # works | services | supplies | ''
    cpv: list[str] = field(default_factory=list)      # тільки цифри 8 знаків
    cpv_labels: dict[str, str] = field(default_factory=dict)
    value_amount: float | None = None
    value_currency: str = ""
    description: str = ""            # уривок предмета замовлення (з тіла)
    body_text: str = ""              # нормалізований текст тіла (для класифікації; в snapshot не пишемо повністю)
    reference: str = ""
    contact_email: str = ""
    lots: int | None = None
    contractors: list[dict[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)  # сирий запис джерела (для архіву/діагностики)

    def primary_key(self) -> str:
        return self.keys[0] if self.keys else f"{self.source}:{self.source_id}"
