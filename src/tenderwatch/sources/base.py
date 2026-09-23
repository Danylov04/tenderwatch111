"""Базовий контракт джерела + захист від «мовчки проігнорованих фільтрів»."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

from ..config import Rules
from ..http import Http
from ..models import Notice


class SourceError(Exception):
    """Джерело відповіло, але результат не можна довіряти (змінилась схема, порожня відповідь, тощо)."""


class FilterIgnored(SourceError):
    """Сервер мовчки проігнорував параметр фільтра (повернув «все підряд»). Дані не приймаємо."""


@dataclass
class Window:
    """Часове вікно, яке треба покрити (UTC)."""
    start: datetime
    end: datetime


@dataclass
class SourceStats:
    fetched: int = 0
    pages: int = 0
    strategies: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    unknown_types: set[str] = field(default_factory=set)

    def count(self, strategy: str, n: int = 1) -> None:
        self.strategies[strategy] = self.strategies.get(strategy, 0) + n
        self.fetched += n


class Source:
    name = "base"

    def __init__(self, http: Http, rules: Rules):
        self.http = http
        self.rules = rules
        self.stats = SourceStats()

    # Швидкі стратегії (ключові слова, CPV) — запускаються часто
    def search(self, window: Window) -> Iterator[Notice]:  # pragma: no cover - інтерфейс
        raise NotImplementedError

    # Повний перегляд вікна (лише для джерел, де це можливо) — запускається щоночі
    def sweep(self, window: Window) -> Iterator[Notice]:
        return iter(())

    # Дозавантаження деталей (тіло оголошення тощо); повертає той самий Notice
    def enrich(self, n: Notice) -> Notice:
        return n

    def doctor(self) -> list[tuple[str, bool, str]]:  # pragma: no cover - інтерфейс
        return []


def check_filter(items: list, predicate: Callable[[object], bool], what: str, tolerance: float = 0.1) -> None:
    """Кидає FilterIgnored, якщо забагато елементів відповіді не відповідають запитаному фільтру.

    Це головний захист від тихої деградації: без нього «фільтр, що ігнорується» дає стабільний потік сміття
    або (що гірше) пропущені тендери, і ніхто цього не помічає.
    """
    if not items:
        return
    bad = sum(1 for it in items if not predicate(it))
    if bad / len(items) > tolerance:
        raise FilterIgnored(f"фільтр {what} не спрацював: {bad}/{len(items)} записів не відповідають")
