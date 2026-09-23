from __future__ import annotations

from ..config import Rules
from ..http import Http
from .base import FilterIgnored, Source, SourceError, SourceStats, Window
from .bk import BkSource
from .ezam import EzamSource
from .pz import PzSource
from .ted import TedSource

REGISTRY: dict[str, type[Source]] = {
    "ezam": EzamSource,
    "ted": TedSource,
    "pz": PzSource,
    "bk": BkSource,
}


def build(name: str, http: Http, rules: Rules) -> Source:
    try:
        return REGISTRY[name](http, rules)
    except KeyError as e:
        raise ValueError(f"невідоме джерело {name!r}; доступні: {sorted(REGISTRY)}") from e


__all__ = ["FilterIgnored", "Source", "SourceError", "SourceStats", "Window", "REGISTRY", "build"]
